"""Loopback-only, zero-spend scripted Reflection provider for real native terminal tests.
Test tooling only: it needs a runtime built with the `test-hooks` feature."""
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

def sample_task(request,stage):
    """The default script: read, edit and check sample.py, then report."""
    if stage==1:
        return "read_file",{"path":"sample.py"}
    if stage==2:
        return "edit_file",{"path":"sample.py","expectedSha256":hashlib.sha256(b"before\n").hexdigest(),"content":"after\n"}
    if stage==3:
        return "run_command",{"command":"test \"$(cat sample.py)\" = after && printf checked > checked.txt","timeout_seconds":5}
    return "Task complete: sample.py updated and checked."

class Fixture:
    def __init__(self,respond=sample_task):
        """`respond(request, stage)` returns reply text or a `(tool_name, arguments)` call."""
        self.requests=[]
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):
                pass
            def do_POST(self):
                length=int(self.headers.get("Content-Length",0))
                if length>512*1024:
                    self.send_error(413)
                    return
                request=json.loads(self.rfile.read(length))
                owner.requests.append(request)
                stage=len(owner.requests)
                action=respond(request,stage)
                content,(name,args)=(action,(None,None)) if isinstance(action,str) else (None,action)
                delta={"content":content} if content else {"tool_calls":[{"index":0,"id":f"call-{stage}","type":"function","function":{"name":name,"arguments":json.dumps(args)}}]}
                payload="data: "+json.dumps({"choices":[{"index":0,"delta":delta,"finish_reason":"stop" if content else "tool_calls"}]})+"\n\n"
                payload+='data: {"choices":[],"usage":{"prompt_tokens":20,"completion_tokens":10}}\n\ndata: [DONE]\n\n'
                self.send_response(200)
                self.send_header("Content-Type","text/event-stream")
                self.send_header("Content-Length",str(len(payload.encode())))
                self.end_headers()
                self.wfile.write(payload.encode())
        self.server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
    def config(self):
        return dict(endpoint=f"http://127.0.0.1:{self.server.server_port}/chat/completions",model="Beam-1.0-501B-A23B",effort="medium",env_file=None,max_output_tokens=1000,max_requests=8)
    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
