"""Run the deterministic recovery rules without API credentials."""
import argparse
import itertools
import json
import tempfile
from collections import defaultdict
from pathlib import Path

from drc.flash_recovery import RULES, external_instances, make_dataset
from drc.flash_revision import REGIMES, run_revision
from drc.sqlite_contracts import FAULTS


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope',choices=('smoke','full'),default='smoke')
    parser.add_argument('--output',type=Path,required=True,help='New JSONL output path')
    args=parser.parse_args()
    instances=[x for x in make_dataset()['instances'] if x['split']=='test']+external_instances()
    if args.scope=='smoke':instances=[instances[0],instances[-1]]
    assert not args.output.exists(),args.output
    args.output.parent.mkdir(parents=True,exist_ok=True)
    groups=defaultdict(lambda: [0,0])
    with tempfile.TemporaryDirectory() as temp,args.output.open('x') as stream:
        for k,(instance,fault,method,regime) in enumerate(itertools.product(instances,FAULTS,(*RULES,'item_rule'),REGIMES)):
            row=run_revision(Path(temp)/f'{k}.db',instance,fault,method,regime)
            stream.write(json.dumps(row,sort_keys=True)+'\n')
            key=(instance['split'],regime,method)
            groups[key][0]+=int(row['metrics']['scts'])
            groups[key][1]+=1
    summary={'scope':args.scope,'episodes':sum(n for _,n in groups.values()),
             'scts_by_group':[{'split':split,'regime':regime,'method':method,'scts':v[0],'n':v[1]}
                              for (split,regime,method),v in sorted(groups.items())]}
    args.output.with_suffix('.summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps({'episodes':summary['episodes'],'output':str(args.output)},indent=2))


if __name__=='__main__':main()
