#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Worker de monitoramento INDEPENDENTE - nao trava a UI."""
import sys, os, time, argparse, signal, threading
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
import monitor
_stop = threading.Event()
def _sigint(*_):
    print("\n[monitor] sinal recebido...")
    _stop.set()
signal.signal(signal.SIGINT, _sigint)
signal.signal(signal.SIGTERM, _sigint)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=int, default=60, help="minutos entre checagens")
    p.add_argument("--once", action="store_true", help="roda 1 vez e sai")
    args = p.parse_args()
    if not monitor.SELENIUM_OK:
        print("[monitor] AVISO: selenium nao instalado (pip install -r requirements.txt)")
    print(f"[monitor] iniciando (intervalo={args.interval}min, once={args.once})")
    if args.once:
        w = monitor.MonitoringWorker(interval_minutes=999)
        w._check_all()
        print("[monitor] 1 checagem concluida, saindo")
        return
    worker = monitor.MonitoringWorker(interval_minutes=args.interval)
    worker.start()
    try:
        while not _stop.is_set():
            _stop.wait(5)
    finally:
        worker.stop()
        monitor._close_driver()
        print("[monitor] encerrado")

if __name__ == "__main__":
    main()
