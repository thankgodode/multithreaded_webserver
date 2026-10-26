use std::{collections::{HashMap, hash_map::Entry}, fs, io::{self, BufRead, BufReader, ErrorKind, Write}, net::{TcpListener, TcpStream}, sync::{Arc, Mutex, OnceLock, mpsc::{self, Receiver, Sender}}, thread::{self, JoinHandle}, time::Duration};

pub const MAX_HEADER_BYTES: usize = 16 * 1024; // request line + all headers
pub const MAX_BODY_BYTES: usize = 1024 * 1024;
const MAX_HEADERS: usize = 100;
/// How long an idle keep-alive connection may hold a worker thread.
const IDLE_TIMEOUT: Duration = Duration::from_secs(5);
type Job = Box<dyn FnOnce() + Send +'static>;
struct ThreadPool{
    sender: Sender<Job>,
    workers: Vec<Workers>
}

impl ThreadPool{
    fn new(size: usize)-> ThreadPool{
        assert!(size> 0);

        let (sender, receiver) = mpsc::channel();        
        let receiver = Arc::new(Mutex::new(receiver));

        let mut workers = Vec::with_capacity(size);

        for id in 0..size{
            workers.push(Workers::new(id, Arc::clone(&receiver)));
        }

        ThreadPool { sender, workers }
    }

    fn execute<F>(&self, f:F)
    where
        F: FnOnce()+ Send + 'static
    {
        let job = Box::new(f);
        self.sender.send(job).unwrap()
    }
}

struct Workers{
    id: usize,
    threads: JoinHandle<()>
}

impl Workers{
    fn new(id: usize, receiver: Arc<Mutex<Receiver<Job>>>)-> Workers{
        let threads = thread::spawn(move|| {
            loop{
                let job = receiver.lock().unwrap().recv().unwrap();

                // println!("Worker {id} executiing task");

                job();
            }
        });

        Workers{ id, threads}
    }
}

#[derive(Debug)]
enum Methods {
    Get,
    Post,
    Put, 
    Delete,
    Options,
    Head
}

impl Methods{
    fn parse(s: &str)-> Option<Methods>{
        Some(match s {
            "GET"=> Methods::Get,
            "POST" => Methods::Post,
            "PUT"=> Methods::Put,
            "DELETE"=> Methods::Delete,
            "HEAD"=> Methods::Head,
            "OPTIONS"=> Methods::Options,
            _=> return None
        })
    }
}

#[derive(PartialEq,Debug)]
enum Version{
    Http11,
    Http10
}

#[derive(Debug)]
struct Requests{
    method: Methods,
    path: String,
    query: Vec<(String, String)>,
    version: Version,
    headers: HashMap<String, String>,
    body: Vec<u8>
}

#[derive(Debug)]
enum ParseError{
    BadRequestLine,
    UnsupportedMethod,
    UnsupportedVersion,
    UnsupportedTransferEncoding,
    BadHeader,
    TooManyHeaders,
    HeadersTooLarge,
    BodyTooLarge,
    MissingHost,
    BadEncoding,
    Io(ErrorKind)
}

impl ParseError{
    fn status(&self) -> u16{
        match self{
            ParseError::UnsupportedMethod | ParseError::UnsupportedTransferEncoding => 501,
            ParseError::UnsupportedVersion =>505,
            ParseError::TooManyHeaders | ParseError::HeadersTooLarge => 431,
            ParseError::BodyTooLarge => 413,
            ParseError::Io(_) => 408,            
            _ =>{
                400
            }
        }
    }

    /// True when the connection went away (or idled out) rather than the
    /// client sending something malformed. In that case there is nobody left
    /// to receive an error response, so the connection is just dropped.
    fn is_disconnect(&self) -> bool{
        matches!(self, ParseError::Io(k) if matches!(k,
            ErrorKind::WouldBlock
            | ErrorKind::TimedOut
            | ErrorKind::UnexpectedEof
            | ErrorKind::BrokenPipe
            | ErrorKind::ConnectionReset
            | ErrorKind::ConnectionAborted))
    }
}

impl From<io::Error> for ParseError{
    fn from(e: io::Error)-> Self{
        ParseError::Io(e.kind())
    }
}

fn main() -> io::Result<()>{
    let listener = TcpListener::bind("127.0.0.1:8080")?;

    let pools = ThreadPool::new(4);

    println!("Server listening on port 8080");

    for stream in listener.incoming(){
        let stream = match stream{
            Ok(stream) => stream,
            Err(_) => panic!("Error unwrapping stream")
        };

        pools.execute(|| {
            if let Err(e) = handle_connections(stream) {
                eprintln!("connection ended: {e:?}");
            }
        });
    }
    
    Ok(())
}

fn read_requests<R: BufRead>(r: &mut R)-> Result<Option<Requests>, ParseError>{
    let mut budget = MAX_HEADER_BYTES;
    let mut line = String::new();

    if read_line(&mut *r, &mut line, &mut budget)? == 0{
        return Ok(None);
    }

    let (method, target, version) = parse_request_line(trim_eol(&line))?;
    let (path, query) = split_target(target)?;

    let mut headers: HashMap<String, String> = HashMap::new();

    loop{
        if read_line(&mut *r, &mut line, &mut budget)? ==0 {
            return Err(io::Error::from(io::ErrorKind::UnexpectedEof).into())
        }

        let l = trim_eol(&line);

        if l.is_empty(){
            break;
        }

        if headers.len() >= MAX_HEADERS{
            return Err(ParseError::TooManyHeaders);
        }

        let (name, value) = parse_header(l)?;
        match headers.entry(name){
            Entry::Vacant(e)=>{
                e.insert(value);
            },
            Entry::Occupied(mut e) =>{
                if e.key() =="content-length" {
                    if e.get() != &value {
                        return Err(ParseError::BadHeader);
                    }
                }else{
                    let v = e.get_mut();
                    
                    v.push_str(", ");
                    v.push_str(&value);
                }
            }
        }
    }

    if version == Version::Http11 && !headers.contains_key("host"){
        return Err(ParseError::MissingHost);
    }

    if headers.contains_key("transfer-encoding"){
        return Err(ParseError::UnsupportedTransferEncoding);
    }

    let body = match headers.get("content-length"){
        Some(v) =>{
            if v.is_empty() || !v.bytes().all(|b| b.is_ascii_digit()){
                return Err(ParseError::BadHeader);
            }

            let len = v.parse().map_err(|_| ParseError::BodyTooLarge)?;

            if len > MAX_BODY_BYTES{
                return Err(ParseError::BodyTooLarge);
            }

            let mut body = vec![0u8; len];

            r.read_exact(&mut body)?;
            body
        }
        None=> Vec::new()
    };

    Ok(Some(Requests{method,path,query,version,headers,body}))
}

fn handle_connections(mut stream: TcpStream)->Result<(), ParseError>{
    // An idle keep-alive connection would otherwise pin a worker thread
    // forever once the client stops sending.
    stream.set_read_timeout(Some(IDLE_TIMEOUT))?;
    // Responses are small; don't wait on Nagle between request and reply.
    let _ = stream.set_nodelay(true);

    // Reading and writing need separate handles because BufReader borrows.
    let mut buf_reader = BufReader::new(stream.try_clone()?);

    loop{
        let request = match read_requests(&mut buf_reader){
            Ok(Some(request)) => request,
            // Clean shutdown by the client between requests.
            Ok(None) => break,
            // Idle timeout or the peer vanished: nothing left to reply to.
            Err(e) if e.is_disconnect() => break,
            Err(e) =>{
                // Protocol error: answer once, then drop the connection
                // because the request framing is no longer trustworthy.
                let body = format!("{}\n", e.status());
                write_response(&mut stream, e.status(), "text/plain", &body, true)?;
                break;
            }
        };

        let close = wants_close(&request);

        let path = &request.path;

        write_response(&mut stream, 200, "text/html", index_contents(path).as_str(), close)?;

        if close{
            break;
        }
    }

    Ok(())
}

/// HTTP/1.1 keeps the connection open unless the client (or we) opt out.
/// HTTP/1.0 is the opposite: it closes unless the client asked for keep-alive.
fn wants_close(request: &Requests)-> bool{
    let opted_out = request
        .headers
        .get("connection")
        .map(|v| v.split(',').any(|t| t.trim().eq_ignore_ascii_case("close")))
        .unwrap_or(false);

    opted_out || (request.version == Version::Http10 && !request.headers.contains_key("connection"))
}

fn index_contents(paths:&String) -> String{

    let content = match paths.as_str(){
        "/" => "./src/hello.html",
        "/about"=> "./src/about.html",
        "/profile"=> "./src/profile.html",
        _=> "/"
    };
    
    fs::read_to_string(content).unwrap_or_default()
}

fn write_response(
    stream: &mut TcpStream,
    status: u16,
    content_type: &str,
    body: &str,
    close: bool,
)-> Result<(), ParseError>{
    let reason = match status{
        200 => "OK",
        400 => "Bad Request",
        408 => "Request Timeout",
        413 => "Payload Too Large",
        431 => "Request Header Fields Too Large",
        501 => "Not Implemented",
        505 => "HTTP Version Not Supported",
        _ => "Error",
    };
    let connection = if close { "close" } else { "keep-alive" };

    let head = format!(
        "HTTP/1.1 {status} {reason}\r\nConnection: {connection}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\n\r\n",
        body.len()
    );

    stream.write_all(head.as_bytes())?;
    stream.write_all(body.as_bytes())?;
    stream.flush()?;

    Ok(())
}

fn read_line<R:BufRead>(
    r: R,
    buf: &mut String,
    budget: &mut usize,
)-> Result<usize, ParseError>{
    buf.clear();

    let n = r.take(*budget as u64 + 1).read_line(buf)?;

    if n > *budget{
        return Err(ParseError::HeadersTooLarge);
    }

    if n > 0 && !buf.ends_with("\n"){
        return Err(io::Error::from(io::ErrorKind::UnexpectedEof).into())
    }

    *budget -= n;

    Ok(n)
}

fn trim_eol(s: &str)-> &str{
    s.trim_end_matches(['\r', '\n'])
}

fn parse_request_line(line: &str)-> Result<(Methods, &str, Version), ParseError>{
    let mut parts = line.split(" ");

    let (m, t,v) = match (parts.next(), parts.next(), parts.next(), parts.next()){
        (Some(m), Some(t),Some(v), None)=> (m,t,v),
        _ => return Err(ParseError::BadRequestLine)
    };

    let method = Methods::parse(m).ok_or(ParseError::UnsupportedMethod)?;

    if !t.starts_with("/"){
        return Err(ParseError::BadRequestLine)
    }

    let version = match v{
        "HTTP/1.1" => Version::Http11,
        "HTTP/1.0" => Version::Http10,
        _=> return Err(ParseError::UnsupportedVersion)
    };

    Ok((method, t, version))
}

fn parse_header(line:&str)-> Result<(String, String), ParseError>{
    let (name, value) = line.split_once(":").ok_or(ParseError::BadHeader)?;

    let bad_name = name.is_empty() || name.bytes().any(|b| b.is_ascii_whitespace() || b.is_ascii_control());

    let bad_value = value.bytes().any(|b| b==0 || (b<0x20 && b !=b'\t'));

    if bad_name || bad_value{
        return Err(ParseError::BadHeader)
    }

    Ok((name.to_ascii_lowercase(), value.trim().to_string()))
}


#[allow(clippy::type_complexity)]
fn split_target(target: &str) -> Result<(String, Vec<(String, String)>), ParseError> {
    let (raw_path, raw_query) = target.split_once('?').unwrap_or((target, ""));
    let path = percent_decode(raw_path, false)?;
    let query = raw_query
        .split('&')
        .filter(|p| !p.is_empty())
        .map(|pair| {
            let (k, v) = pair.split_once('=').unwrap_or((pair, ""));
            Ok((percent_decode(k, true)?, percent_decode(v, true)?))
        })
        .collect::<Result<Vec<_>, ParseError>>()?;
    Ok((path, query))
}

/// `plus_as_space` is true for query strings, false for paths.
fn percent_decode(s: &str, plus_as_space: bool) -> Result<String, ParseError> {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        match b[i] {
            b'%' => {
                let hex = b.get(i + 1..i + 3).ok_or(ParseError::BadEncoding)?;
                out.push(hex_val(hex[0])? << 4 | hex_val(hex[1])?);
                i += 3;
            }
            b'+' if plus_as_space => {
                out.push(b' ');
                i += 1;
            }
            c => {
                out.push(c);
                i += 1;
            }
        }
    }
    String::from_utf8(out).map_err(|_| ParseError::BadEncoding)
}

fn hex_val(c: u8) -> Result<u8, ParseError> {
    match c {
        b'0'..=b'9' => Ok(c - b'0'),
        b'a'..=b'f' => Ok(c - b'a' + 10),
        b'A'..=b'F' => Ok(c - b'A' + 10),
        _ => Err(ParseError::BadEncoding),
    }
}