from __future__ import annotations
import argparse
from http.server import ThreadingHTTPServer
from pathlib import Path
from src.http_api import make_handler
from src.repository import Repository
from src.service import Service
def parse_args():
    parser=argparse.ArgumentParser(description='大坝巡检、缺陷与应急管理')
    parser.add_argument("--db",default="./data.db",help="SQLite数据库路径")
    parser.add_argument("--port",type=int,default=8316,help="HTTP端口")
    parser.add_argument("--host",default="127.0.0.1",help="监听地址")
    return parser.parse_args()
def main():
    args=parse_args(); repository=Repository(args.db); service=Service(repository)
    server=ThreadingHTTPServer((args.host,args.port),make_handler(service,str(Path(__file__).resolve().parent/"static")))
    print(f"listening on http://{args.host}:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close(); repository.close()
if __name__=="__main__": main()
