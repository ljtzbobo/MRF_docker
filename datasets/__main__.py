"""python -m datasets: scan, list, generate, validate and run dataset-backed tasks."""
import argparse
import json
from pathlib import Path

from .catalog import DatasetCatalog,DatasetError
from .generator import generate,compatibility


def write(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parent)
    sub=parser.add_subparsers(dest='command',required=True)
    scan=sub.add_parser('scan');scan.add_argument('--hash',action='store_true')
    sub.add_parser('list')
    check=sub.add_parser('validate');check.add_argument('--task',type=Path,required=True)
    verify=sub.add_parser('verify');verify.add_argument('--record',required=True)
    for name in ('generate','run'):
        p=sub.add_parser(name);p.add_argument('--scenario',required=True);p.add_argument('--record',required=True)
        p.add_argument('--experiment',type=Path);p.add_argument('--output',type=Path,default=Path('outputs/dataset-task.json' if name=='generate' else 'outputs/dataset-run.json'))
        if name=='run':
            p.add_argument('--deployment',type=Path,required=True);p.add_argument('--keep-warm',action='store_true')
    args=parser.parse_args();catalog=DatasetCatalog(args.root)
    try:
        if args.command=='scan':
            from .inventory import scan
            data=scan(args.root,hash_files=args.hash)
            print(json.dumps(dict(summary=data['summary'],warnings=data['warnings']),ensure_ascii=False,indent=2));return
        if args.command=='list':
            data=catalog.read()
            for r in data['records']:print(r['record_id'],r['adapter'],r['status'],r['title'])
            return
        if args.command=='validate':
            task=json.loads(args.task.read_text(encoding='utf-8'))
            print(json.dumps(compatibility(task),ensure_ascii=False,indent=2));return
        if args.command=='verify':
            from .inventory import digest
            record=catalog.record(args.record)
            actual,_=digest(catalog.source(record))
            if actual!=record.get('sha256'):raise DatasetError('source SHA256 differs from the indexed revision')
            print(json.dumps(dict(record_id=args.record,sha256=actual,verified=True)));return
        experiment=json.loads(args.experiment.read_text(encoding='utf-8')) if args.experiment else {}
        bundle=generate(catalog,args.scenario,args.record,experiment)
        bundle['compatibility']=compatibility(bundle['task'])
        if args.command=='generate':
            write(args.output,bundle['task']);write(args.output.with_name(args.output.stem+'.binding.json'),bundle)
        else:
            from .execution import DatasetFrames,evaluate
            from deployment.config import load_deployment_manager
            from matching import match_task
            task=bundle['task'];match=match_task(task);frames=DatasetFrames(catalog,task)
            manager=load_deployment_manager(args.deployment)
            try:
                from matching.selection import select_composition,ReboundFrames
                preview=manager.pool.preview if args.keep_warm else manager.scheduler.preview_placement
                chosen,_,_=select_composition(match,lambda spec,candidate:preview(spec,candidate,frame_count=len(frames)))
                if chosen.execution_graph!=match.execution_graph:frames=ReboundFrames(frames,match.execution_graph,chosen.execution_graph)
                result=manager.execute(chosen.specification_task,chosen,frames,keep_warm=args.keep_warm)
                write(args.output,dict(task=task,match=chosen.to_dict(),plan=result.plan.to_dict(),evaluation=evaluate(result),
                    source_kind='downloaded_recording',execution_mode=result.execution_mode,
                    resource_profile_note='Placement timings/capacities follow the selected deployment configuration.'))
            finally:
                manager.pool.close();manager.pool.shutdown()
        print(args.output.resolve())
        print(json.dumps(bundle['compatibility'],ensure_ascii=False))
    except (DatasetError,ValueError,OSError) as exc:parser.exit(2,str(exc)+'\n')


if __name__=='__main__':main()
