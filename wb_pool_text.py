# -*- coding: utf-8 -*-
"""文本号池：只服务文本模型调用的派发模块。

与 ``wb_pool_media`` 成对存在，两者共用同一个 ``wb_accounts.AccountPool``
实例（账号文件、凭据刷新、出口代理、日额度护栏都只有一份），但**派发边界**
各自独立：

* 本模块只会从 ``poolScope == "text"`` 的账号里选号；
* 任何拿到手的账号都要再过一遍 ``require()``，媒体号池的账号在这里被挡回去，
  并抛 ``wb_pool_scope.ScopeViolation``（403），从而保证「媒体号池的账号不能
  发起文本模型请求」不是靠调用方自觉，而是这一层硬拒。

为什么要再挡一次：选号之外还有几条路径会**拿现成的账号**去发文本请求——面板
钉住的账号、API Key 绑定的账号、会话亲和已经绑定的账号、签到 / 每日活跃这类
定时任务、以及重试链里沿用的上一轮账号。这些路径都不过 ``pick()``，只靠选号
过滤等于给媒体账号留了一条后门。
"""
import wb_pool_scope as scope_mod


class PoolBoundaryBlocked(Exception):
    """这道入口绑定的号池不接受这个账号（见 wb_pool_scope.boundary）。

    与 ``ScopeViolation`` 的区别：那条是账号字段判定的 403，这条是部署层面
    「本进程根本不该跑这个号池的活」，HTTP 层翻译成 503（本入口无此项服务），
    并引导客户端换用另一个入口。
    """

    def __init__(self, message, scope="", wanted=""):
        super().__init__(message)
        self.scope = scope or ""
        self.wanted = wanted or ""
        self.code = "POOL_BOUNDARY"

    @property
    def status(self):
        return 503


class TextPool(object):
    """文本号池的门面：选号 + 准入检查。"""

    def __init__(self, pool, log=None):
        self.pool = pool
        self.log = log or (lambda msg: None)

    # ---------------------------------------------------------------- 选号
    def pick(self, realm=None, exclude=None, model=None, page=None, allowed=None):
        """从文本号池里挑一个账号，没有可用账号时返回 None。

        ``allowed`` 是 API Key 的账号白名单，``exclude`` 是本轮已经试过的账号，
        两者都由调用方传进来，这里只叠加号池这一维。
        """
        if self.pool is None:
            return None
        if not scope_mod.separation_enabled():
            return self.pool.pick(realm=realm, exclude=exclude, model=model,
                                  page=page, allowed=allowed)
        if not scope_mod.entry_allows(scope_mod.SCOPE_TEXT):
            # 媒体入口：本进程不服务任何文本模型请求，直接没有候选。
            return None
        return self.pool.pick(realm=realm, exclude=exclude, model=model,
                              page=page, allowed=allowed,
                              scope=scope_mod.SCOPE_TEXT)

    def pick_for_session(self, realm=None, session_key=None, exclude=None,
                         model=None, page=None, allowed=None):
        """一轮对话的选号：优先沿用这条会话上一轮用的账号。"""
        if self.pool is None:
            return None
        if not scope_mod.separation_enabled():
            return self.pool.pick_for_session(
                realm=realm, session_key=session_key, exclude=exclude,
                model=model, page=page, allowed=allowed)
        if not scope_mod.entry_allows(scope_mod.SCOPE_TEXT):
            return None
        return self.pool.pick_for_session(
            realm=realm, session_key=session_key, exclude=exclude,
            model=model, page=page, allowed=allowed,
            scope=scope_mod.SCOPE_TEXT)

    def count_ready(self, realm=None, model=None, allowed=None, refresh=True):
        """文本号池里此刻能用的账号数（面板 / 总并发都用这个口径）。"""
        if self.pool is None:
            return 0
        if not scope_mod.separation_enabled():
            return self.pool.count_ready(realm, model=model, allowed=allowed,
                                         refresh=refresh)
        if not scope_mod.entry_allows(scope_mod.SCOPE_TEXT):
            return 0
        return self.pool.count_ready(realm, model=model, allowed=allowed,
                                     refresh=refresh, scope=scope_mod.SCOPE_TEXT)

    # ---------------------------------------------------------------- 准入
    def require(self, account):
        """确认这个账号可以跑文本模型，不可以就抛 ScopeViolation。

        返回账号本身，方便 ``account = TEXTS.require(account)`` 这样串联。
        """
        if account is None:
            return None
        if not scope_mod.entry_allows(scope_mod.SCOPE_TEXT):
            raise PoolBoundaryBlocked(
                "这个入口只服务图片 / 视频生成，不发起文本模型请求；"
                "文本模型请走文本号池的入口。",
                scope=scope_mod.boundary() or "", wanted=scope_mod.SCOPE_TEXT)
        if scope_mod.separation_enabled() and \
                scope_mod.account_scope(account) == scope_mod.SCOPE_MEDIA:
            raise scope_mod.text_violation(account)
        return account

    def allows(self, account):
        """不抛异常的版本，给「过滤候选列表」用。"""
        if account is None:
            return False
        if not scope_mod.separation_enabled():
            return True
        if not scope_mod.entry_allows(scope_mod.SCOPE_TEXT):
            return False
        return scope_mod.account_scope(account) != scope_mod.SCOPE_MEDIA

    def accounts(self):
        """文本号池的账号列表（快照）。定时任务按号遍历时用它过滤。"""
        if self.pool is None:
            return []
        with self.pool._lock:
            values = list(self.pool.accounts)
        return [a for a in values if self.allows(a)]

    def get(self, uid):
        """按 uid 取文本号池里的账号；属于媒体号池则返回 None。"""
        if self.pool is None:
            return None
        account = self.pool.get(uid)
        return account if self.allows(account) else None

    def public(self, realm=None):
        """面板账号列表（只含文本号池）。"""
        return [a for a in self.pool.list_public(realm) if self.allows_public(a)]

    @staticmethod
    def allows_public(row):
        """对 ``Account.public()`` 出来的 dict 做同样的判定。"""
        if not scope_mod.separation_enabled():
            return True
        if not scope_mod.entry_allows(scope_mod.SCOPE_TEXT):
            return False
        return scope_mod.normalize_scope(row.get("poolScope")) != scope_mod.SCOPE_MEDIA
