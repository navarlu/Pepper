#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import print_function

import datetime
import json

try:
    # Python 3
    from urllib.request import Request, urlopen
except Exception:
    # Python 2
    from urllib2 import Request, urlopen


BRIDGE_URL = "http://127.0.0.1:5000"
ENDPOINT = "/tablet/text_inline"


def main():
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload = {
        "text": "Tablet smoke test\n{}".format(now),
        "size": 56,
        "bg": "#0B1020",
        "fg": "#CDEBFF",
        "align": "center",
    }

    data = json.dumps(payload).encode("utf-8")
    req = Request(
        "{}{}".format(BRIDGE_URL, ENDPOINT),
        data=data,
        headers={"Content-Type": "application/json"},
    )

    try:
        resp = urlopen(req, timeout=3.0)
        body = resp.read()
        print("tablet request: OK")
        print(body)
    except Exception as exc:
        print("tablet request: FAILED")
        print(exc)
        raise


if __name__ == "__main__":
    main()
