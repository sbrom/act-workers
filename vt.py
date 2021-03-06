#!/usr/bin/env python3

'''VirusTotal worker for the ACT platform

Copyright 2018 the ACT project <opensource@mnemonic.no>

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH
REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND
FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR
OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
PERFORMANCE OF THIS SOFTWARE.

requirements:

    https://github.com/blacktop/virustotal-api

    pip install virustotal-api
'''

from __future__ import print_function

import argparse
import collections
import contextlib
import ipaddress
import re
import sys
import warnings
from logging import error
import traceback
import act
from functools import partialmethod

import requests
from virus_total_apis import PublicApi as VirusTotalApi

AV_HEURISTICS = ['trojan', 'adware', 'dropper', 'miner',
                 'backdoor', 'malware', 'downloader', 'rat',
                 'hacktool', 'ransomware', 'cryptolocker',
                 'banker', 'financial', 'eicar', 'scanner']

ADWARE_OVERRIDES = ['opencandy', 'monetize', 'adload', 'somoto']

MS_RE = re.compile(r"(.*?):(.*?)\/(?:([^!.]+))?(?:[!.](\w+))?")
KASPERSKY_RE = re.compile(r"((.+?):)?(.+?)\.(.+?)\.([^.]+)(\.(.+))?")
VERSION = "{}.{}".format(sum(1 for x in [False, set(), ["Y"], {}, 0] if x), sum(1 for y in [False] if y))


def parse_args():
    """Extract command lines argument"""

    parser = argparse.ArgumentParser(description='ACT VT Client v{}'.format(VERSION))
    parser.add_argument('--apikey', metavar='KEY', type=str,
                        required=True, help='VirusTotal API key')
    parser.add_argument('--proxy', metavar='PROXY', type=str,
                        help='set the system proxy')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--hexdigest', action='store_true',
                       default=False, help='query hexdigestsum on stdin')
    group.add_argument('--ip', action='store_true',
                       default=False, help='query ip on stdin')
    group.add_argument('--domain', action='store_true',
                       default=False, help='query domain on stdin')

    parser.add_argument('--userid', dest='user_id', required=True,
                        help="User ID")
    parser.add_argument('--act-baseurl', dest='act_baseurl', required=True,
                        help='ACT API URI')
    parser.add_argument("--logfile", dest="logfile",
                        help="Log to file (default = stdout)")
    parser.add_argument("--loglevel", dest="loglevel", default="info",
                        help="Loglevel (default = info)")

    return parser.parse_args()


def name_extraction(engine, body):
    """Extract the name from certain AV engines based on regular
    expression matching"""

    if engine == "Microsoft":
        match = MS_RE.match(body["result"])
        if match:
            return match.groups()[2].lower()

    if engine == "Kaspersky":
        match = KASPERSKY_RE.match(body["result"])
        if match:
            return match.groups()[4].lower()

    return None


def is_adware(text):
    """Test for adware signature using heuristics in ADWARE_OVERRIDES"""

    for adware_override in ADWARE_OVERRIDES:
        if adware_override in text:
            return True
    return False


def handle_hexdigest(actapi, vtapi, hexdigest):
    """Read hexdigest from stdin, query VirusTotal and
    output a JSON text readable by generic_uploader.py"""

    names = set()
    kind = collections.Counter()
    with no_ssl_verification():
        response = vtapi.get_file_report(hexdigest)

    if 'scans' not in response['results']:
        # VirusTotal has not seend this hexdigest before
        return

    for engine, body in response['results']['scans'].items():
        if not body['detected']:
            continue

        name = name_extraction(engine, body)
        if name:
            names.add(name)

        res = body['result'].lower()

        if is_adware(res):
            names.add('adware')

        for heur in AV_HEURISTICS:
            if heur in res:
                # Add a vote for this heuristic
                kind[heur] += 1

    # Decide on malware "kind" based on popular vote among the
    # names extracted from the AV_HEURISTICS.
    if kind:
        names.add(kind.most_common()[0][0])

    for name in names:
        actapi.fact("isTool", "vt")\
            .source("hash", hexdigest)\
            .destination("tool", name)\
            .add()


def handle_ip(actapi, vtapi, ip):
    """Read IP address from stdin, query VirusTotal and
    output a JSON text readable by generic_uploaderr.py"""

    # To figure out what kind of IP address we have, let the ipaddress module
    # parse the string and test for instance type as the platform distinguishes
    # between IPv4 and IPv6 addresses.
    try:
        ip_address = ipaddress.ip_address(ip)
    except ValueError as err:
        return  # invalid address

    if isinstance(ip_address, ipaddress.IPv4Address):
        ip_type = 'ipv4'
    elif isinstance(ip_address, ipaddress.IPv6Address):
        ip_type = 'ipv6'
    else:
        return  # if it is an unknown type, abort early. No query will happen.

    with no_ssl_verification():
        response = vtapi.get_ip_report(ip)

    try:
        results = response['results']
    except KeyError:
        print(response, "in handle_ip for", ip)
        sys.exit(1)

    if 'resolutions' in results:
        for resolution in results['resolutions']:
            actapi.fact("DNSRecord", "A")\
                .source("fqdn", resolution["hostname"])\
                .destination(ip_type, ip)\
                .add()

    if 'detected_downloaded_samples' in results:
        for sample in results['detected_downloaded_samples']:
            actapi.fact("observation")\
                .source("hash", sample["sha256"])\
                .destination(ip_type, ip)\
                .add()

    if 'detected_communicating_samples' in results:
        for sample in results['detected_communicating_samples']:
            actapi.fact("usesC2", ip_type)\
                .source("hash", sample["sha256"])\
                .destination(ip_type, ip)\
                .add()


def handle_domain(actapi, vtapi, domain):
    """Read IP address from stdin, query VirusTotal and
    output a JSON text readable by generic_uploaderr.py"""

    with no_ssl_verification():
        response = vtapi.get_domain_report(domain)

    try:
        results = response['results']
    except KeyError:
        print(response, "in handle_domain for", domain)
        sys.exit(1)

    if 'resolutions' in results:
        for resolution in results['resolutions']:
            ip = resolution['ip_address']
            # To figure out what kind of IP address we have, let the ipaddress module
            # parse the string and test for instance type as the platform distinguishes
            # between IPv4 and IPv6 addresses.
            try:
                ip_address = ipaddress.ip_address(ip)
            except ValueError as err:
                continue  # invalid address

            if isinstance(ip_address, ipaddress.IPv4Address):
                ip_type = 'ipv4'
            elif isinstance(ip_address, ipaddress.IPv6Address):
                ip_type = 'ipv6'
            else:
                continue  # if it is an unknown type, abort early. No query will happen.

            actapi.fact("DNSRecord", "A")\
                .source("fqdn", domain)\
                .destination(ip_type, ip)\
                .add()

    if 'detected_downloaded_samples' in results:
        for sample in results['detected_downloaded_samples']:
            actapi.fact("observation")\
                .source("hash", sample["sha256"])\
                .destination("fqdn", domain)\
                .add()

    if 'detected_communicating_samples' in results:
        for sample in results['detected_communicating_samples']:
            actapi.fact("usesC2", "fqdn")\
                .source("hash", sample["sha256"])\
                .destination("fqdn", domain)\
                .add()


def main():
    """main function"""

    args = parse_args()

    actapi = act.Act(args.act_baseurl, args.user_id, args.loglevel, args.logfile, "vt-enrichment")

    in_data = sys.stdin.read().strip()

    proxies = {
        'http': args.proxy,
        'https': args.proxy
    } if args.proxy else None

    vtapi = VirusTotalApi(args.apikey, proxies=proxies)

    if args.hexdigest:
        handle_hexdigest(actapi, vtapi, in_data)

    if args.ip:
        handle_ip(actapi, vtapi, in_data)

    if args.domain:
        handle_domain(actapi, vtapi, in_data)


@contextlib.contextmanager
def no_ssl_verification():
    """Monkey patch request to default to no verification of ssl"""

    old_request = requests.Session.request
    requests.Session.request = partialmethod(old_request, verify=False)

    warnings.filterwarnings('ignore', 'Unverified HTTPS request')
    yield
    warnings.resetwarnings()

    requests.Session.request = old_request


if __name__ == '__main__':

    try:
        main()
    except Exception as e:
        error("Unhandled exception: {}".format(traceback.format_exc()))
        raise
