from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from py_mini_racer import MiniRacer

_ACRAWLER_JS = Path(__file__).with_name("acrawler.js")

_SHIM = """
var global = globalThis, window = globalThis, self = globalThis;
var noop = function () {};
var mem = {};
globalThis.document = {
    cookie: "", referrer: "",
    createElement: function () { return { getContext: function () { return null; }, style: {} }; },
    addEventListener: noop, removeEventListener: noop,
    documentElement: { style: {} }, body: { appendChild: noop, style: {} },
    getElementById: function () { return null; }, querySelector: function () { return null; }
};
globalThis.navigator = {
    userAgent: %(ua)s, platform: "Win32", language: "zh-CN", languages: ["zh-CN"],
    hardwareConcurrency: 8, webdriver: false, plugins: [], mimeTypes: [], cookieEnabled: true
};
globalThis.location = {
    protocol: "https:", href: "https://www.douyin.com/", host: "www.douyin.com",
    hostname: "www.douyin.com", pathname: "/", origin: "https://www.douyin.com",
    reload: noop, search: "", hash: ""
};
globalThis.localStorage = {
    getItem: function (k) { return mem[k] || null; },
    setItem: function (k, v) { mem[k] = String(v); },
    removeItem: function (k) { delete mem[k]; }
};
globalThis.sessionStorage = { getItem: function () { return null; }, setItem: noop, removeItem: noop };
globalThis.screen = { width: 1536, height: 864 };
globalThis.performance = { timing: { navigationStart: Date.now() }, now: function () { return Date.now(); } };
globalThis.history = { length: 1 };
globalThis.Image = function () {};
globalThis.PluginArray = function () {};
globalThis.indexedDB = {};
globalThis.DOMException = Error;
globalThis.WebSocket = function () {};
globalThis.Request = function () {};
globalThis.Headers = function () {};
"""


@lru_cache(maxsize=1)
def _acrawler_source() -> str:
    return _ACRAWLER_JS.read_text(encoding="utf-8")


def sign_ac(nonce: str, user_agent: str) -> str:
    ctx = MiniRacer()
    ctx.eval(_SHIM % {"ua": json.dumps(user_agent)})
    ctx.eval(_acrawler_source())
    ctx.eval("byted_acrawler.init({aid:99999999,dfp:0})")
    signature = ctx.eval("byted_acrawler.sign('', %s)" % json.dumps(nonce))
    if not isinstance(signature, str) or len(signature) < 40:
        raise RuntimeError("抖音 __ac_signature 生成失败")
    return signature
