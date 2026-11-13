"""Real Docker cold/Warm/CRIU-restore comparison; never invent missing samples."""
import argparse
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import random
import statistics
from time import perf_counter
from uuid import uuid4

import numpy as np
from components.runtime import DataPacket
from deployment.checkpoints import DockerCheckpoints
from deployment.docker_runtime import DockerRuntime
from resources.measurements import PhysicalUsage
from resources.placement import PlacementAllocation


def metrics(runtime,handle):
    # Probe CPU counters and current RSS-accounted memory, not an invented percent.
    raw=runtime._docker('exec',handle.container_id,'sh','-c',
        "cat /sys/fs/cgroup/cpu.stat; echo MEMORY; cat /sys/fs/cgroup/memory.current; echo INODE; stat -c %i /sys/fs/cgroup/cpu.stat")
    cpu,rest=raw.split('MEMORY\n');memory,inode=rest.split('INODE\n')
    usage=int(next(line.split()[1] for line in cpu.splitlines() if line.startswith('usage_usec ')))
    return dict(cpu_us=usage,memory_bytes=int(memory.strip()),counter_inode=int(inode.strip()))


def delta(before,after):
    if before is None or before['counter_inode']!=after['counter_inode']:return after['cpu_us']
    return max(0,after['cpu_us']-before['cpu_us'])


def run(image,repetitions,output):
    runtime=DockerRuntime('experiment-cpu',{'C03':image})
    info=json.loads(runtime._docker('info','--format','{{json .}}'))
    image_id=runtime.pool_identity('C03')
    node=dict(id='ddc',cid='C03',parameters=dict(mode='ddc',nco_frequency=100000.,decimation=2),
              inputs={'in':'iq/complex'},outputs={'out':'iq/baseband'})
    template=PlacementAllocation('ddc','C03','C03-cpu-experiment','',runtime.processing_node_id,'cpu',
        PhysicalUsage(cpu_millicores=1000,memory_bytes=256*2**20),1,1,
        runtime_identity=image_id,lease_id='experiment')
    packet=DataPacket(np.exp(2j*np.pi*300000*np.arange(4096)/4e6),4e6,100e6,0,'time')
    store=DockerCheckpoints(runtime,output.parent/'benchmark-checkpoints.sqlite')
    rows=[];warm=None;expected=None
    def fresh():return replace(template,container_unit_id='experiment-'+uuid4().hex,lease_id=uuid4().hex)
    try:
        a=fresh();start=perf_counter();warm=runtime.start(a,node);warm_seed_ms=(perf_counter()-start)*1000
        for repetition in range(repetitions):
            modes=['cold_start','warm_reuse','restore'];random.Random(42+repetition).shuffle(modes)
            for mode in modes:
                handle=None;allocation=fresh();before=None;checkpoint_ms=None
                row=dict(repetition=repetition,mode=mode,status='failed')
                try:
                    if mode=='warm_reuse':
                        handle=warm;allocation=replace(a,lease_id=uuid4().hex)
                        before=metrics(runtime,handle)
                    elif mode=='restore':
                        handle=runtime.start(allocation,node)
                        before=metrics(runtime,handle)
                        cpstart=perf_counter();manifest=store.save(handle,allocation,node)
                        checkpoint_ms=(perf_counter()-cpstart)*1000
                        store.claim(handle.unit_id,allocation.lease_id)
                    started=perf_counter()
                    if mode=='cold_start':handle=runtime.start(allocation,node)
                    elif mode=='warm_reuse':runtime.health(handle);runtime.reset(handle,allocation,node)
                    else:handle=store.restore(allocation,node)
                    ready_ms=(perf_counter()-started)*1000
                    ready=metrics(runtime,handle)
                    process_started=perf_counter()
                    result=runtime.process(handle,{'in':packet})
                    process_ms=(perf_counter()-process_started)*1000
                    after=metrics(runtime,handle)
                    digest=sha256(result.samples.tobytes()).hexdigest()
                    if expected is None:expected=digest
                    if digest!=expected:raise RuntimeError('restored/reset signal output differs from cold reference')
                    row.update(status='ok',prepare_ms=ready_ms,process_ms=process_ms,
                        preparation_cpu_us=delta(before,ready),process_cpu_us=delta(ready,after),
                        memory_ready_bytes=ready['memory_bytes'],memory_after_bytes=after['memory_bytes'],
                        checkpoint_ms=checkpoint_ms,output_sha256=digest)
                except Exception as exc:row['error']=str(exc)
                finally:
                    if handle is not None and handle!=warm:
                        try:runtime.stop(handle)
                        except Exception as exc:row['cleanup_error']=str(exc)
                    rows.append(row)
                    print(json.dumps(row,ensure_ascii=False),flush=True)
        summaries={}
        for mode in ('cold_start','warm_reuse','restore'):
            valid=[r for r in rows if r['mode']==mode and r['status']=='ok' and 'cleanup_error' not in r]
            summaries[mode]=dict(successful=len(valid),attempted=repetitions)
            for key in ('prepare_ms','preparation_cpu_us','process_cpu_us','memory_ready_bytes'):
                summaries[mode]['median_'+key]=statistics.median(r[key] for r in valid) if valid else None
        baseline=summaries['cold_start']
        for mode in ('warm_reuse','restore'):
            for key in ('prepare_ms','preparation_cpu_us','memory_ready_bytes'):
                old=baseline['median_'+key];new=summaries[mode]['median_'+key]
                summaries[mode][key+'_reduction_fraction']=None if old is None or not old or new is None else 1-new/old
        report=dict(schema_version=1,status='complete' if all(s['successful']==repetitions for s in summaries.values()) else 'incomplete',
            environment={k:info.get(k) for k in ('ID','OSType','KernelVersion','Architecture','ServerVersion','ExperimentalBuild','Driver','CgroupVersion')},
            image_id=image_id,component=node,quota=dict(cpu_millicores=1000,memory_bytes=256*2**20),
            workload='4096 synthetic complex IQ samples; one reset component call per trial',
            measurement_notes=['Real Docker wall time and cgroup-v2 counters; probes contribute CPU overhead.',
                'CPU counter inode detects cgroup recreation after restore. Memory is current usage, not peak RSS.',
                'Cold uses a cached local image; build/pull cost excluded. Warm seed and checkpoint creation reported separately.',
                'Median of successful trials only; failed restore is never a zero-time sample or a cold substitute.'],
            warm_seed_ms=warm_seed_ms,summary=summaries,trials=rows)
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        return report
    finally:
        if warm is not None:runtime.stop(warm)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',default='mrf-component:checkpoint-test')
    parser.add_argument('--repetitions',type=int,default=5)
    parser.add_argument('--output',type=Path,default=Path('outputs/snapshot-benchmark.json'))
    args=parser.parse_args()
    if args.repetitions<3:parser.error('at least three repetitions are required')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    report=run(args.image,args.repetitions,args.output)
    print(json.dumps(dict(status=report['status'],summary=report['summary']),ensure_ascii=False,indent=2))
    print(args.output.resolve())

if __name__=='__main__':main()
