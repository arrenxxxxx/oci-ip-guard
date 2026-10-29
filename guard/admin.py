import json
import os
import sys
from .common import request, secret

command = sys.argv[1] if len(sys.argv)>1 else 'status'
if command not in ['status','events','switch','reconcile','test-notification']:
    raise SystemExit('Commands: status, events, switch IP, reconcile, test-notification')
payload = None if command in ['status','events'] else {'ip':sys.argv[2]} if command=='switch' else {}
print(json.dumps(request('http://127.0.0.1:8765/'+command,payload,
                        {'Authorization':'Bearer '+secret('ADMIN_TOKEN')}),indent=2))
