#!/usr/bin/env python3
"""Generate and execute independent M5 runs; every failed seed is retained."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from simulator.control import Control
from simulator.m3_validation import batch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--count', type=int, default=100)
    parser.add_argument('--backend', choices=['sqlite', 'postgresql'], default='sqlite')
    parser.add_argument('--pg-url-file')
    parser.add_argument('--purpose', choices=['validation', 'regression', 'development'], default='validation')
    parser.add_argument('--seed-start', type=int)
    args = parser.parse_args()
    result = batch(Control.initialize(args.root, args.pg_url_file), args.count, args.backend, args.purpose, args.seed_start, stage='M5')
    print(json.dumps({k: v for k, v in result.items() if k != 'cases'}, ensure_ascii=False))
    raise SystemExit(bool(result['failed']))


if __name__ == '__main__': main()
