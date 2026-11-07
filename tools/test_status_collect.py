#!/usr/bin/env python3
"""Offline test for status_collect.parse_stalls: a fixed tailscaled journal, no journalctl, no network."""
import datetime
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import status_collect as sc

now = datetime.datetime(2026, 11, 7, 2, 30, tzinfo=datetime.timezone.utc)
J = '''2026-11-07T02:17:58+00:00 srv tailscaled[1]: open-conn-track: timeout opening (TCP 100.109.138.51:53210 => 100.64.243.30:57800) to node [aNsSl]; online=yes, lastRecv=1s
2026-11-07T02:10:02+00:00 srv tailscaled[1]: open-conn-track: timeout opening (TCP 100.109.138.51:40334 => 100.64.243.30:22) to node [aNsSl]; online=yes, lastRecv=6s
2026-11-06T21:10:00+00:00 srv tailscaled[1]: open-conn-track: timeout opening (TCP 100.109.138.51:40335 => 100.64.243.30:22) to node [aNsSl]; online=yes, lastRecv=4s
2026-11-06T18:13:23+00:00 srv tailscaled[1]: open-conn-track: timeout opening (TCP 100.109.138.51:51028 => 100.66.41.62:22) to node [HK7C6]; online=yes, lastRecv=3s
2026-11-06T18:13:24+00:00 srv tailscaled[1]: open-conn-track: timeout opening (TCP 100.109.138.51:51029 => 100.66.41.62:22) to node [HK7C6]; online=no, lastRecv=900s
2026-11-05T18:00:00+00:00 srv tailscaled[1]: open-conn-track: timeout opening (TCP 100.109.138.51:51030 => 100.66.41.62:22) to node [HK7C6]; online=yes, lastRecv=3s
2026-11-07T02:18:25+00:00 srv tailscaled[1]: magicsock: disco: node [aNsSl] now using 77.40.3.85:9619
'''
p = sc.parse_stalls(J, {'100.64.243.30': 'pinpin-mac-mini'}, now)
fails = []
if [x['peer'] for x in p] != ['pinpin-mac-mini', '100.66.41.62']: fails.append(f'peers {p}')
m = p[0]
if (m['last_hour'], m['day'], m['last'], m['ports']) != (2, 3, '2026-11-07T02:17:58Z', {'57800': 1, '22': 2}): fails.append(f'mini {m}')
if (p[1]['last_hour'], p[1]['day']) != (0, 1): fails.append(f'air: offline and >24 h not counted {p[1]}')
if sc.parse_stalls('', None, now) != []: fails.append('empty journal')
print('\n'.join(fails) or 'status_collect: all checks passed')
sys.exit(1 if fails else 0)
