
#!/usr/bin/env python3
"""Probe KS S3 tick data format: directory structure, CSV fields, sampling frequency."""
import sys, os, io, json, tarfile, logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.basicConfig(level=logging.WARNING)

# Load credentials safely
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'credentials.json'), 'r') as f:
    creds = json.load(f)

from urllib.parse import urlparse
endpoint = urlparse(creds.get('url', '')).hostname
if endpoint:
    endpoint = endpoint + ':6900'
else:
    endpoint = creds.get('endpoint', '').replace('http://', '').replace('https://', '').split('/')[0]

from minio import Minio

client = Minio(
    endpoint,
    access_key=creds.get('accessKey', creds.get('access_key', '')),
    secret_key=creds.get('secretKey', creds.get('secret_key', '')),
    secure=False,
)
bucket = 'ks-data'

# 1. List tick dates for SHFE.rb
print("=== Step 1: List tick dates for SHFE.rb ===")
prefix = 'SHFE/rb/L1/tick/'
tick_dates = []
for obj in client.list_objects(bucket, prefix=prefix, recursive=False):
    name = obj.object_name[len(prefix):]
    date_part = name.split('.')[0].rstrip('/')
    if date_part.isdigit() and len(date_part) == 8:
        tick_dates.append(int(date_part))
tick_dates.sort()
print(f"Total tick dates: {len(tick_dates)}")
if tick_dates:
    print(f"First date: {tick_dates[0]}, Last date: {tick_dates[-1]}")
    print(f"Sample dates: {tick_dates[:5]} ... {tick_dates[-5:]}")

# 2. Download the most recent tick tar and inspect
if tick_dates:
    test_date = tick_dates[-1]  # most recent
    s3_key = f'SHFE/rb/L1/tick/{test_date}.tar.gz'
    print(f"\n=== Step 2: Download tick tar for {test_date} ===")
    print(f"S3 key: {s3_key}")
    
    response = client.get_object(bucket, s3_key)
    tar_data = response.read()
    response.close()
    print(f"Downloaded: {len(tar_data)} bytes ({len(tar_data)/1024/1024:.1f} MB)")
    
    # 3. Extract and inspect CSV content
    print(f"\n=== Step 3: Inspect tick tar contents ===")
    tf = tarfile.open(fileobj=io.BytesIO(tar_data), mode='r:gz')
    members = tf.getmembers()
    print(f"Total files in tar: {len(members)}")
    
    # Show first 10 file names
    csv_members = [m for m in members if m.name.endswith('.csv')]
    print(f"CSV files: {len(csv_members)}")
    for m in csv_members[:10]:
        print(f"  {m.name} ({m.size} bytes)")
    if len(csv_members) > 10:
        print(f"  ... and {len(csv_members) - 10} more")
    
    # 4. Read first CSV and inspect format
    if csv_members:
        print(f"\n=== Step 4: Inspect first CSV format ===")
        first_member = csv_members[0]
        f = tf.extractfile(first_member)
        content = f.read().decode('utf-8-sig')
        lines = content.strip().split('\n')
        print(f"File: {first_member.name}")
        print(f"Total lines: {len(lines)}")
        
        # Header
        if lines:
            header = lines[0]
            print(f"Header: {header}")
            fields = header.split(',')
            print(f"Fields ({len(fields)}): {fields}")
        
        # First 5 data lines
        for i, line in enumerate(lines[1:6], 1):
            print(f"  L{i}: {line}")
        
        # Last 3 data lines
        if len(lines) > 6:
            print(f"  ...")
            for line in lines[-3:]:
                print(f"  L{len(lines)}: {line}")
        
        # Time range analysis
        if len(lines) > 2 and 'time' in header.lower():
            time_idx = header.split(',').index('time') if 'time' in header.split(',') else -1
            if time_idx >= 0:
                first_time = lines[1].split(',')[time_idx]
                last_time = lines[-1].split(',')[time_idx]
                print(f"\nTime range: {first_time} -> {last_time}")
                print(f"Total ticks in file: {len(lines) - 1}")
                
                # Estimate sampling interval from first 20 ticks
                times = []
                for line in lines[1:21]:
                    try:
                        t = int(line.split(',')[time_idx])
                        times.append(t)
                    except:
                        pass
                if len(times) >= 2:
                    diffs = [times[i+1] - times[i] for i in range(len(times)-1)]
                    non_zero_diffs = [d for d in diffs if d > 0]
                    if non_zero_diffs:
                        print(f"Tick intervals (first 20): min={min(non_zero_diffs)}, max={max(non_zero_diffs)}, "
                              f"avg={sum(non_zero_diffs)/len(non_zero_diffs):.0f} ms")
    
    tf.close()

# 5. Check another product (CFFEX.IF) for comparison
print(f"\n=== Step 5: Check CFFEX.IF tick dates ===")
prefix2 = 'CFFEX/IF/L1/tick/'
tick_dates2 = []
for obj in client.list_objects(bucket, prefix=prefix2, recursive=False):
    name = obj.object_name[len(prefix2):]
    date_part = name.split('.')[0].rstrip('/')
    if date_part.isdigit() and len(date_part) == 8:
        tick_dates2.append(int(date_part))
tick_dates2.sort()
print(f"CFFEX.IF tick dates: {len(tick_dates2)}")
if tick_dates2:
    print(f"Range: {tick_dates2[0]} - {tick_dates2[-1]}")

# 6. Check a CZCE product
print(f"\n=== Step 6: Check CZCE.AP tick dates ===")
prefix3 = 'CZCE/AP/L1/tick/'
tick_dates3 = []
for obj in client.list_objects(bucket, prefix=prefix3, recursive=False):
    name = obj.object_name[len(prefix3):]
    date_part = name.split('.')[0].rstrip('/')
    if date_part.isdigit() and len(date_part) == 8:
        tick_dates3.append(int(date_part))
tick_dates3.sort()
print(f"CZCE.AP tick dates: {len(tick_dates3)}")
if tick_dates3:
    print(f"Range: {tick_dates3[0]} - {tick_dates3[-1]}")
    # Download one to compare format
    test_date3 = tick_dates3[-1]
    s3_key3 = f'CZCE/AP/L1/tick/{test_date3}.tar.gz'
    response3 = client.get_object(bucket, s3_key3)
    tar_data3 = response3.read()
    response3.close()
    tf3 = tarfile.open(fileobj=io.BytesIO(tar_data3), mode='r:gz')
    csv_members3 = [m for m in tf3.getmembers() if m.name.endswith('.csv')]
    print(f"  {test_date3}: {len(tar_data3)/1024/1024:.1f}MB, {len(csv_members3)} CSV files")
    if csv_members3:
        f3 = tf3.extractfile(csv_members3[0])
        content3 = f3.read().decode('utf-8-sig')
        lines3 = content3.strip().split('\n')
        if lines3:
            print(f"  Header: {lines3[0]}")
            if len(lines3) > 1:
                print(f"  L1: {lines3[1]}")
    tf3.close()

# 7. Size estimation for a single day
print(f"\n=== Step 7: Single day tar size estimation ===")
# Use a mid-range date for rb
if tick_dates:
    mid_idx = len(tick_dates) // 2
    mid_date = tick_dates[mid_idx]
    s3_key_mid = f'SHFE/rb/L1/tick/{mid_date}.tar.gz'
    try:
        stat = client.stat_object(bucket, s3_key_mid)
        print(f"SHFE.rb {mid_date}: {stat.size/1024/1024:.1f} MB (compressed)")
    except:
        print(f"SHFE.rb {mid_date}: not found")
