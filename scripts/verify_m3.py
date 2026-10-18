#!/usr/bin/env python3
"""Run fresh M3 acceptance datasets; keep failed cases and all reports."""
import sys,argparse,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from simulator.control import Control
from simulator.m3_validation import batch

def main():
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--count',type=int,default=100)
    p.add_argument('--backend',choices=['sqlite','postgresql'],default='sqlite');p.add_argument('--pg-url-file')
    p.add_argument('--purpose',choices=['validation','regression','development'],default='validation');p.add_argument('--seed-start',type=int)
    a=p.parse_args();result=batch(Control.initialize(a.root,a.pg_url_file),a.count,a.backend,a.purpose,a.seed_start)
    print(json.dumps({k:v for k,v in result.items() if k!='cases'},ensure_ascii=False));raise SystemExit(bool(result['failed']))
if __name__=='__main__':main()
