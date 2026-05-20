import utils
import sys
import io

if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

for cid in ['66046', '66554', '66891', '67005', '67225', '67170', '67096']:
    try:
        videoList, name, _ = utils.get_course_info(cid)
        for v in videoList:
            if str(v.get('id', '')) == '857190':
                print(f"FOUND! Course ID {cid} is {name}, session {v.get('title')}")
                break
    except Exception as e:
        pass
