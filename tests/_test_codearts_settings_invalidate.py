"""`/settings/save` 的 CodeArts 分支：只在目录相关开关变化时清模型缓存。

旧实现无条件调用 `wb_codearts.invalidate_models()`，把每个账号的模型目录
清空；下一次 `/v1/models` 会重新打三路上游发现。调一个冷却秒数或超时也
付这份成本，属于纯浪费。本套钉住：
  - 改冷却/超时等非目录字段 → 不清缓存；
  - 改 enabled / models_ttl_seconds / benefit_auto_claim → 清缓存。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="wb-codearts-invalidate-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_codearts
import wb_proxy as proxy


class FakeRequest(object):
    """Just enough of the handler for the settings-save branch."""

    _handle_settings_save = proxy.Handler._handle_settings_save

    def __init__(self, payload):
        self.payload = payload
        self.answering = []

    def _payload_or_error(self, allow_list=False):
        return self.payload

    def _error(self, status, message, kind=""):
        self.answering.append(("error", status, message))
        return status, message

    def _json(self, status, payload):
        self.answering.append(("json", status, payload))
        return status, payload


class CodeArtsSettingsInvalidateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wb-codearts-settings-")
        old = proxy.ACCOUNTS_DIR
        proxy.ACCOUNTS_DIR = self.dir
        self.addCleanup(setattr, proxy, "ACCOUNTS_DIR", old)
        self.calls = []
        self._orig = wb_codearts.invalidate_models
        wb_codearts.invalidate_models = lambda: self.calls.append("models")
        self.addCleanup(setattr, wb_codearts, "invalidate_models", self._orig)
        wb_codearts.save_config(self.dir, {"enabled": True})

    def save(self, patch):
        request = FakeRequest({"codearts": patch})
        request._handle_settings_save()
        for kind, status, _ in request.answering:
            if kind == "error":
                self.fail("save rejected with %s" % status)
        return self.calls

    def test_non_catalogue_fields_do_not_clear_the_model_cache(self):
        calls = self.save({"timeout_seconds": 45, "stream_timeout_seconds": 900})
        self.assertEqual(calls, [], calls)

    def test_unknown_fields_do_not_clear_the_model_cache(self):
        calls = self.save({"max_concurrent": 5, "busy_wait_seconds": 3})
        self.assertEqual(calls, [], calls)

    def test_toggling_enabled_clears_the_model_cache(self):
        self.save({"enabled": True})       # already True -> no change
        self.assertEqual(self.calls, [], self.calls)
        calls = self.save({"enabled": False})
        self.assertEqual(calls, ["models"], calls)

    def test_changing_models_ttl_clears_the_model_cache(self):
        calls = self.save({"models_ttl_seconds": 120})
        self.assertEqual(calls, ["models"], calls)

    def test_toggling_benefit_auto_claim_clears_the_model_cache(self):
        calls = self.save({"benefit_auto_claim": False})
        self.assertEqual(calls, ["models"], calls)


if __name__ == "__main__":
    unittest.main()
