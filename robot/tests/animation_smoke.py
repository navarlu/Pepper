#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import print_function

import json

try:
    # Python 3
    from urllib.request import Request, urlopen
except Exception:
    # Python 2
    from urllib2 import Request, urlopen


BRIDGE_URL = "http://127.0.0.1:5000"
ANIMATION_NAME = "Happy_3"


def main():
    url = "{}/animation/{}".format(BRIDGE_URL, ANIMATION_NAME)
    req = Request(url, data=b"{}", headers={"Content-Type": "application/json"})
    try:
        resp = urlopen(req, timeout=8.0)
        body = resp.read()
        print("animation smoke: OK")
        try:
            print(json.loads(body))
        except Exception:
            print(body)
    except Exception as exc:
        print("animation smoke: FAILED")
        print(exc)
        raise


if __name__ == "__main__":
    main()
