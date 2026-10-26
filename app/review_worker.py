"""Continuous recovery of durable analysis jobs, with bounded retry in review.py."""
import argparse,json,logging,os,signal,time
from threading import Event,Lock,Thread
from . import db,review
from .config import settings_for
from .migrations import migrate

logger=logging.getLogger(__name__)


class Supervisor:
    def __init__(self,engine,*,interval=2.0,batch_size=20):
        if not .01<=interval<=300 or not 1<=batch_size<=100:raise ValueError('Invalid analysis worker interval/batch size')
        self.engine=engine;self.interval=interval;self.batch_size=batch_size
        self.stop_event=Event();self.lock=Lock();self.thread=None
        self.state=dict(enabled=True,alive=False,scanning=False,last_started_at=None,last_finished_at=None,
            scans=0,reports_observed=0,last_error=None,poll_seconds=interval)

    def status(self):
        with self.lock:return dict(self.state,alive=bool(self.thread and self.thread.is_alive()))

    def cycle(self):
        with self.lock:self.state.update(scanning=True,last_started_at=time.time())
        try:
            results=review.recover(self.engine,self.batch_size,self.stop_event)
            with self.lock:
                self.state['scans']+=1;self.state['reports_observed']+=sum(bool(x) for x in results)
                self.state.update(last_error=None,last_finished_at=time.time())
            return results
        except Exception as exc:
            with self.lock:self.state.update(last_error='QUEUE_UNAVAILABLE',last_finished_at=time.time())
            logger.warning('analysis_queue_error type=%s',type(exc).__name__)
            return []
        finally:
            with self.lock:self.state['scanning']=False

    def run(self):
        logger.info('analysis_worker_started poll_seconds=%s',self.interval)
        while not self.stop_event.is_set():
            self.cycle()
            self.stop_event.wait(self.interval)
        logger.info('analysis_worker_stopped')

    def start(self):
        if self.thread and self.thread.is_alive():return self
        self.stop_event.clear();self.thread=Thread(target=self.run,name='analysis-recovery',daemon=True);self.thread.start();return self

    def stop(self,timeout=10):
        self.stop_event.set()
        if self.thread:self.thread.join(timeout)
        if self.thread and self.thread.is_alive():logger.warning('analysis_worker_shutdown_waiting_for_transaction')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--expected-run-id',required=True)
    parser.add_argument('--watch',action='store_true')
    parser.add_argument('--interval',type=float,default=2)
    parser.add_argument('--batch-size',type=int,default=20)
    args=parser.parse_args()
    if not os.environ.get('APP_DATA_DIR'):parser.error('Set APP_DATA_DIR explicitly to the intended runtime')
    if not 0.1<=args.interval<=300 or not 1<=args.batch_size<=100:parser.error('interval: 0.1..300; batch-size: 1..100')
    engine=db.make_engine();worker=Supervisor(engine,interval=args.interval,batch_size=args.batch_size)
    try:
        if settings_for(engine).run_id!=args.expected_run_id:parser.error('Runtime identifier does not match --expected-run-id')
        migrate(engine)
        if not args.watch:
            result=worker.cycle();print(json.dumps(dict(attempted=len(result),reports_observed=sum(bool(x) for x in result),status=worker.status())));return
        logging.basicConfig(level=logging.INFO,format='%(message)s')
        for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:worker.stop_event.set())
        worker.run()
    finally:engine.dispose()


if __name__=='__main__':main()
