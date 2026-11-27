"""免费模型不受低余额保留线拦截。

修复前：免费名单只认目录里的 ``credits: x0.00``。``hy4flash-free`` 这类
上游按名字标了 free、但没进定价目录的模型因此不在名单里；账户余额跌破
保留线后，连它也被 park，网关对所有模型回 503——付费余额低，免费模型
跟着停。

本套钉住三件事：

  - ``model_id_is_free``：``credits`` 是主信号，明确非零价格永远优先；
    只有价格缺失时才使用经过确认的免费名单（不是任意 ``*free`` 名称）。
  - ``free_models_by_realm``：三个来源（内置快照 / 桌面缓存 / 活目录）
    都走同一判定。
  - 账号层：余额低于保留线时，确认的免费模型仍 ``ready``，付费模型、
    名称带 free 但未确认的模型仍被挡。

No network: both remote-catalogue readers are stubbed in every test.
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="wb-free-model-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_accounts
import wb_proxy as P


class ModelIdIsFreeTests(unittest.TestCase):
    def test_credits_zero_means_free(self):
        for value in ("x0.00", "x0", "0", "0.00", "  X0.00  "):
            with self.subTest(value=value):
                self.assertTrue(P.model_id_is_free("some-model", {"credits": value}))

    def test_confirmed_free_ids_without_price_are_free(self):
        self.assertTrue(P.model_id_is_free("hy4flash-free", None))
        self.assertTrue(P.model_id_is_free("Hy4Flash-FREE", {}))
        self.assertTrue(P.model_id_is_free(" hy4free ", None))

    def test_nonzero_price_wins_over_a_free_name(self):
        # 明确非零价格 + 名称带 free：付费判定必须优先，保留线不能被绕过。
        self.assertFalse(P.model_id_is_free("hy4flash-free", {"credits": "x0.30"}))
        self.assertFalse(P.model_id_is_free("hy4free", {"credits": "x9.99"}))

    def test_paid_and_unconfirmed_models_are_not_free(self):
        self.assertFalse(P.model_id_is_free("deepseek-v4.1-flash", {"credits": "x1.00"}))
        self.assertFalse(P.model_id_is_free("glm-5.3", None))
        self.assertFalse(P.model_id_is_free("hy4std", {"credits": ""}))
        self.assertFalse(P.model_id_is_free("", None))
        self.assertFalse(P.model_id_is_free(None, None))
        # 任意 *free 名称不等于确认的免费模型
        self.assertFalse(P.model_id_is_free("made-up-free", None))
        self.assertFalse(P.model_id_is_free("hy4flash", {"credits": "x0.30"}))


class FreeModelsByRealmTests(unittest.TestCase):
    def setUp(self):
        P._free_models_cache.update({"at": 0.0, "data": None})
        self._orig_live = (dict(P._models_cache), dict(P._catalog_meta))
        self._orig_cached = P.read_cached_remote_catalog
        P.read_cached_remote_catalog = lambda realm: None

    def tearDown(self):
        P._models_cache.clear()
        P._models_cache.update(self._orig_live[0])
        P._catalog_meta.clear()
        P._catalog_meta.update(self._orig_live[1])
        P.read_cached_remote_catalog = self._orig_cached
        P._free_models_cache.update({"at": 0.0, "data": None})

    def test_confirmed_free_id_without_price_enters_free_set(self):
        P._models_cache["cn"] = {"at": 0.0, "data": [
            ("hy4flash-free", {}),
            ("deepseek-v4.1-flash", {"credits": "x1.00"}),
            ("hy3", {"credits": "x0.00"}),
        ]}
        free = P.free_models_by_realm()["cn"]
        self.assertIn("hy4flash-free", free)
        self.assertIn("hy3", free)
        self.assertNotIn("deepseek-v4.1-flash", free)

    def test_confirmed_free_ids_enter_free_set_even_when_absent_from_catalogue(self):
        P._models_cache["intl"] = {"at": 0.0, "data": []}
        P._models_cache["cn"] = {"at": 0.0, "data": []}
        view = P.free_models_by_realm()
        for realm in ("intl", "cn"):
            self.assertIn("hy4flash-free", view[realm])
            self.assertIn("hy4free", view[realm])

    def test_paid_price_in_cached_catalogue_beats_a_free_name(self):
        P._models_cache["cn"] = {"at": 0.0, "data": []}
        P.read_cached_remote_catalog = lambda realm: (
            ["hy4flash-free", "glm-5.3"],
            {"hy4flash-free": {"credits": "x0.30"}, "glm-5.3": {"credits": "x0.79"}},
        ) if realm == "cn" else None
        free = P.free_models_by_realm()["cn"]
        self.assertNotIn("hy4flash-free", free)
        self.assertNotIn("glm-5.3", free)

    def test_higher_layer_paid_price_overrides_static_free_and_confirmed_ids(self):
        # cached 把静态免费 hy3 标价、且忽略大小写移除 hy4flash-free；live 即使只给空价格也不能误加回。
        P.read_cached_remote_catalog = lambda realm: (
            ["Hy4Flash-Free", "hy3"],
            {"Hy4Flash-Free": {"credits": "x0.30"}, "hy3": {"credits": "x0.25"}},
        ) if realm == "intl" else None
        P._models_cache["intl"] = {"at": 0.0, "data": [
            ("hy4flash-free", {}),
            ("hy4free", {"credits": "x0.50"}),
            ("made-up-free", {}),
        ]}
        free = P.free_models_by_realm()["intl"]
        self.assertNotIn("hy4flash-free", free)
        self.assertNotIn("hy3", free)
        self.assertNotIn("hy4free", free)
        self.assertNotIn("made-up-free", free)

    def test_live_zero_price_restores_model_even_if_cached_marked_paid(self):
        P.read_cached_remote_catalog = lambda realm: (
            ["hy4flash-free"],
            {"hy4flash-free": {"credits": "x0.30"}},
        ) if realm == "intl" else None
        P._models_cache["intl"] = {"at": 0.0, "data": [
            ("hy4flash-free", {"credits": "x0.00"}),
        ]}
        free = P.free_models_by_realm()["intl"]
        self.assertIn("hy4flash-free", free)


class ReserveExemptsFreeModelTests(unittest.TestCase):
    def setUp(self):
        P._free_models_cache.update({"at": 0.0, "data": None})
        self._orig_live = (dict(P._models_cache), dict(P._catalog_meta))
        self._orig_cached = P.read_cached_remote_catalog
        P.read_cached_remote_catalog = lambda realm: None
        P._models_cache["intl"] = {"at": 0.0, "data": []}
        P._models_cache["cn"] = {"at": 0.0, "data": []}

    def tearDown(self):
        P._models_cache.clear()
        P._models_cache.update(self._orig_live[0])
        P._catalog_meta.clear()
        P._catalog_meta.update(self._orig_live[1])
        P.read_cached_remote_catalog = self._orig_cached
        P._free_models_cache.update({"at": 0.0, "data": None})

    def make_account(self, remain, free_models=None, realm="intl"):
        account = wb_accounts.Account({
            "uid": "uid-free-reserve-test",
            "accessToken": "tok-test",
            "realm": realm,
            "credits": {"remain": remain, "used": 1, "size": 100},
        })
        if free_models is None:
            with tempfile.TemporaryDirectory(prefix="wb-free-pool-") as directory:
                pool = wb_accounts.AccountPool(directory, log=lambda _m: None)
                pool.accounts = [account]
                pool.apply_reserve_credits(30, P.free_models_by_realm())
        else:
            account.reserve_credits = 30
            account.free_models = frozenset(free_models)
        return account

    def test_free_model_serves_below_reserve(self):
        account = self.make_account(10, ["hy4flash-free"])
        self.assertFalse(account.reserve_blocked("hy4flash-free"))
        self.assertFalse(account.reserve_blocked("Hy4Flash-FREE"))

    def test_confirmed_free_id_serves_without_catalogue_entry(self):
        # hy4flash-free / hy4free 不在定价目录里：经 free_models_by_realm + apply_reserve_credits 放行
        account = self.make_account(10)
        self.assertFalse(account.reserve_blocked("hy4flash-free"))
        self.assertFalse(account.reserve_blocked("Hy4Flash-FREE"))
        self.assertFalse(account.reserve_blocked("hy4free"))

    def test_catalogue_paid_price_re_enables_reserve_even_for_confirmed_ids(self):
        P._models_cache["intl"] = {"at": 0.0, "data": [
            ("Hy4Flash-Free", {"credits": "x0.30"}),
            ("hy3", {"credits": "x0.25"}),
        ]}
        account = self.make_account(10)
        self.assertTrue(account.reserve_blocked("hy4flash-free"))
        self.assertTrue(account.reserve_blocked("Hy4Flash-FREE"))
        self.assertTrue(account.reserve_blocked("hy3"))
        self.assertFalse(account.reserve_blocked("hy4free"))

    def test_empty_account_free_models_stays_fail_closed(self):
        # 账号层不绕过 free_models：目录未注入时维持 fail-closed
        account = self.make_account(10, [])
        self.assertTrue(account.reserve_blocked("hy4flash-free"))
        self.assertTrue(account.reserve_blocked("hy4free"))

    def test_paid_model_still_blocked_below_reserve(self):
        account = self.make_account(10)
        self.assertTrue(account.reserve_blocked("deepseek-v4.1-flash"))

    def test_unconfirmed_free_name_still_blocked_below_reserve(self):
        # 任意 *free 名称不能绕过保留线
        account = self.make_account(10)
        self.assertTrue(account.reserve_blocked("made-up-free"))
        self.assertTrue(account.reserve_blocked("paid-model-free"))

    def test_reserve_off_never_blocks(self):
        account = self.make_account(1)
        account.reserve_credits = 0
        self.assertFalse(account.reserve_blocked("deepseek-v4.1-flash"))


class BalanceGuardExemptsFreeModelTests(unittest.TestCase):
    """余额保护（balance_until）只拦付费模型。

    上游说「账户积分用尽」时会把账号 park 到次日重置。但免费模型
    （credits x0.00）不花积分，上游对它们照常接单；把整账号一起封掉会让
    「0 积分账号 + 免费模型」这个合法组合整整哑到次日 04:00。
    """

    def make_account(self, free_models=None):
        account = wb_accounts.Account({
            "uid": "uid-balance-free-test",
            "accessToken": "tok-test",
            "realm": "cn",
            "credits": {"remain": 0, "used": 100, "size": 100},
        })
        if free_models is not None:
            account.free_models = frozenset(free_models)
        return account

    def test_free_model_ignores_the_balance_park(self):
        account = self.make_account(["hy4flash-free"])
        account.balance_until = time.time() + 3600
        self.assertEqual(account.throttle_wait("hy4flash-free"), 0.0)
        self.assertEqual(account.throttle_wait("Hy4Flash-FREE"), 0.0)

    def test_paid_model_still_respects_the_balance_park(self):
        account = self.make_account(["hy4flash-free"])
        account.balance_until = time.time() + 3600
        self.assertGreater(account.throttle_wait("deepseek-v4.1-flash"), 3000)

    def test_unconfirmed_free_name_does_not_bypass_the_balance_park(self):
        account = self.make_account(["hy4flash-free"])
        account.balance_until = time.time() + 3600
        self.assertGreater(account.throttle_wait("made-up-free"), 3000)

    def test_diagnostic_string_hides_the_balance_park_for_a_free_model(self):
        account = self.make_account(["hy4flash-free"])
        account.balance_until = time.time() + 3600
        self.assertEqual(account.unavailable_reason("hy4flash-free"), "")
        self.assertIn("余额保护", account.unavailable_reason("deepseek-v4.1-flash"))


class PublishFreeModelsTests(unittest.TestCase):
    """publish_free_models() 必须把名单真的推到账号层。

    旧代码只在 reserve 护栏打开时（apply_reserve_credits 的 on 分支）才推，
    本地 reserve=0 的部署因此每个账号 free_models 都是空集，余额保护把
    hy4flash-free 一并拦掉。
    """

    def setUp(self):
        P._free_models_cache.update({"at": 0.0, "data": None})
        P._FREE_MODELS_PUBLISHED["sig"] = None
        self._orig_live = (dict(P._models_cache), dict(P._catalog_meta))
        self._orig_cached = P.read_cached_remote_catalog
        P.read_cached_remote_catalog = lambda realm: None
        P._models_cache["intl"] = {"at": 0.0, "data": []}
        P._models_cache["cn"] = {"at": 0.0, "data": []}

    def tearDown(self):
        P._models_cache.clear()
        P._models_cache.update(self._orig_live[0])
        P._catalog_meta.clear()
        P._catalog_meta.update(self._orig_live[1])
        P.read_cached_remote_catalog = self._orig_cached
        P._free_models_cache.update({"at": 0.0, "data": None})

    def test_publish_pushes_the_free_list_even_with_reserve_off(self):
        account = wb_accounts.Account({
            "uid": "uid-publish-free-test",
            "accessToken": "tok-test",
            "realm": "cn",
            "credits": {"remain": 0, "used": 100, "size": 100},
        })
        with tempfile.TemporaryDirectory(prefix="wb-publish-pool-") as directory:
            pool = wb_accounts.AccountPool(directory, log=lambda _m: None)
            pool.accounts = [account]
            orig_pool = P.POOL
            orig_dir = P.ACCOUNTS_DIR
            P.POOL = pool
            P.ACCOUNTS_DIR = os.environ["ACCOUNTS_DIR"]
            try:
                P.publish_free_models(force=True)
            finally:
                P.POOL = orig_pool
                P.ACCOUNTS_DIR = orig_dir
        self.assertIn("hy4flash-free", account.free_models)
        self.assertIn("hy3", account.free_models)

    def test_second_publish_is_a_no_op_without_a_signature_change(self):
        calls = []

        class FakePool(object):
            accounts = []

            def apply_reserve_credits(self, values, free):
                calls.append(free)

        orig_pool = P.POOL
        orig_dir = P.ACCOUNTS_DIR
        P.POOL = FakePool()
        P.ACCOUNTS_DIR = os.environ["ACCOUNTS_DIR"]
        try:
            P.publish_free_models(force=True)
            P.publish_free_models()
            P.publish_free_models()
        finally:
            P.POOL = orig_pool
            P.ACCOUNTS_DIR = orig_dir
        self.assertEqual(len(calls), 1, len(calls))


if __name__ == "__main__":
    unittest.main()
