"""跟单引擎配置：JSON 文件 → 带校验的 dataclass。

完整示例见仓库根目录 config.example.json，各字段含义见 README。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from ..data_api import normalize_address

logger = logging.getLogger(__name__)


class ConfigError(ValueError):
    """配置文件缺失、格式错误或数值非法。"""


def _positive(name: str, value: Any, *, allow_none: bool = True) -> float | None:
    if value is None:
        if allow_none:
            return None
        raise ConfigError(f"{name} 不能为空")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} 应为数字，实际是 {value!r}") from None
    if number <= 0:
        raise ConfigError(f"{name} 应为正数，实际是 {number}")
    return number


def _build(cls, raw: dict[str, Any], section: str):
    """按 dataclass 字段挑选参数构造，未知键只警告不报错（防手滑写错键名）。"""
    if not isinstance(raw, dict):
        raise ConfigError(f"{section} 应为对象，实际是 {type(raw).__name__}")
    known = {f.name for f in fields(cls)}
    # 下划线开头的键当注释用（如 _note），静默忽略；其余未知键才警告（防手滑）
    unknown = {k for k in raw if k not in known and not k.startswith("_")}
    if unknown:
        logger.warning("配置 %s 中有未知字段将被忽略: %s", section, ", ".join(sorted(unknown)))
    return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass
class TargetConfig:
    """一个跟单目标地址。"""

    address: str
    ratio: float | None = None            # 覆盖全局跟单比例
    max_per_trade_usdc: float | None = None  # 覆盖全局单笔上限
    paused: bool = False                  # 暂停跟单（仍会监控与镜像持仓）

    def __post_init__(self) -> None:
        self.address = normalize_address(self.address)
        self.ratio = _positive("targets[].ratio", self.ratio)
        self.max_per_trade_usdc = _positive("targets[].max_per_trade_usdc", self.max_per_trade_usdc)
        self.paused = bool(self.paused)


@dataclass
class SizingConfig:
    """跟单金额怎么算。"""

    mode: str = "proportional"      # proportional：目标金额×ratio；fixed：固定 fixed_usdc
    ratio: float = 0.1
    fixed_usdc: float = 20.0
    max_per_trade_usdc: float = 100.0
    depth_aware: bool = False       # 买入时按盘口深度封顶（并可放大），见 depth.py
    max_follow_multiple: float = 1.0  # 深度放大上限：基准量最多顶到几倍（1=只封顶不放大）

    def __post_init__(self) -> None:
        if self.mode not in ("proportional", "fixed"):
            raise ConfigError(f"sizing.mode 只支持 proportional / fixed，实际是 {self.mode!r}")
        self.ratio = _positive("sizing.ratio", self.ratio, allow_none=False)
        self.fixed_usdc = _positive("sizing.fixed_usdc", self.fixed_usdc, allow_none=False)
        self.max_per_trade_usdc = _positive(
            "sizing.max_per_trade_usdc", self.max_per_trade_usdc, allow_none=False
        )
        self.depth_aware = bool(self.depth_aware)
        self.max_follow_multiple = _positive(
            "sizing.max_follow_multiple", self.max_follow_multiple, allow_none=False
        )
        if self.max_follow_multiple < 1.0:
            raise ConfigError(
                f"sizing.max_follow_multiple={self.max_follow_multiple} 应 ≥ 1"
                "（<1 是缩量，用 ratio 表达）"
            )


@dataclass
class FilterConfig:
    """信号级过滤。"""

    min_target_notional_usdc: float = 10.0  # 目标成交金额低于此不跟（尘埃单）
    max_signal_age_s: float = 30.0          # 信号超龄不跟（价格早已走掉）
    long_horizon_age_s: float | None = 120.0  # 长线市场放宽后的时效上限；null 关闭分级
    long_horizon_days: float = 7.0            # 距市场结束 ≥ 此天数才算长线
    follow_sells: bool = True               # 是否跟随卖出（需要持仓镜像）
    skip_title_patterns: list[str] = field(default_factory=list)  # 市场标题命中即不跟（短期盘）

    def __post_init__(self) -> None:
        self.min_target_notional_usdc = _positive(
            "filters.min_target_notional_usdc", self.min_target_notional_usdc, allow_none=False
        )
        self.max_signal_age_s = _positive(
            "filters.max_signal_age_s", self.max_signal_age_s, allow_none=False
        )
        self.long_horizon_age_s = _positive(
            "filters.long_horizon_age_s", self.long_horizon_age_s
        )
        self.long_horizon_days = _positive(
            "filters.long_horizon_days", self.long_horizon_days, allow_none=False
        )
        if (
            self.long_horizon_age_s is not None
            and self.long_horizon_age_s < self.max_signal_age_s
        ):
            raise ConfigError(
                "filters.long_horizon_age_s 应 ≥ max_signal_age_s（长线是放宽，不是收紧）"
            )
        self.follow_sells = bool(self.follow_sells)
        if not isinstance(self.skip_title_patterns, list):
            raise ConfigError("filters.skip_title_patterns 应为字符串数组")
        # 统一小写，匹配时不分大小写；空串忽略
        self.skip_title_patterns = [
            str(p).lower() for p in self.skip_title_patterns if str(p).strip()
        ]


@dataclass
class RiskConfig:
    """风控闸门。上限设为 null 表示不启用该项。"""

    max_market_exposure_usdc: float | None = 200.0
    max_total_exposure_usdc: float | None = 1000.0
    daily_max_loss_usdc: float | None = 100.0
    market_blacklist: list[str] = field(default_factory=list)  # condition id 或 slug
    kill_switch_file: str = "STOP"  # 该文件存在时全面停止开新仓

    def __post_init__(self) -> None:
        self.max_market_exposure_usdc = _positive(
            "risk.max_market_exposure_usdc", self.max_market_exposure_usdc)
        self.max_total_exposure_usdc = _positive(
            "risk.max_total_exposure_usdc", self.max_total_exposure_usdc)
        self.daily_max_loss_usdc = _positive("risk.daily_max_loss_usdc", self.daily_max_loss_usdc)
        if not isinstance(self.market_blacklist, list):
            raise ConfigError("risk.market_blacklist 应为字符串数组")
        self.market_blacklist = [str(x).lower() for x in self.market_blacklist]


@dataclass
class ExecutionConfig:
    """执行参数。"""

    slippage_cap: float = 0.02  # 相对目标成交价最多多付/少收多少（限价上限）
    retry_no_fill_s: float | None = 3.0  # 限价内无对手盘时隔几秒重试一次；null 不重试

    def __post_init__(self) -> None:
        cap = _positive("execution.slippage_cap", self.slippage_cap, allow_none=False)
        if cap >= 0.5:
            raise ConfigError(f"execution.slippage_cap={cap} 明显过大（价格量纲是 0~1）")
        self.slippage_cap = cap
        self.retry_no_fill_s = _positive("execution.retry_no_fill_s", self.retry_no_fill_s)
        if self.retry_no_fill_s is not None and self.retry_no_fill_s > 15:
            raise ConfigError(
                f"execution.retry_no_fill_s={self.retry_no_fill_s} 过大：重试会阻塞引擎线程，"
                "请 ≤ 15 秒"
            )


@dataclass
class AggregateConfig:
    """信号聚合与轧差（M3）。"""

    window_s: float = 2.0            # 聚合窗口；0 或 null 表示不聚合、逐笔跟单
    net_across_targets: bool = True  # 同窗口内多目标对同一 token 的反向信号轧差
    idle_flush_s: float = 0.5        # 窗口内静默多久提前收批；0 或 null = 等满窗口

    def __post_init__(self) -> None:
        if self.window_s in (None, 0, 0.0):
            self.window_s = 0.0
        else:
            window = _positive("aggregate.window_s", self.window_s, allow_none=False)
            if window > 30:
                raise ConfigError(
                    f"aggregate.window_s={window} 过大：窗口是跟单延迟的下限，"
                    "超过 30s 会让大量信号超龄"
                )
            self.window_s = window
        self.net_across_targets = bool(self.net_across_targets)
        if self.idle_flush_s in (None, 0, 0.0):
            self.idle_flush_s = 0.0
        else:
            # 碎片建仓的连发间隔是毫秒级：静默 0.5s 基本等于「不会再来了」，
            # 提前收批把单笔信号的固定延迟从整个窗口降到静默阈值
            self.idle_flush_s = _positive(
                "aggregate.idle_flush_s", self.idle_flush_s, allow_none=False
            )


@dataclass
class HealthConfig:
    """在跟目标的健康巡检：scout 是入场时的招聘，这是入场后的试用期考核。

    周期性用 scout 的排除规则复查每个在跟目标（回放最近成交带 + 当前持仓），
    命中排除规则（死仓暴增/频率突变/胜率崩/长期不活跃等）自动暂停该目标，
    防止「跟的人变质了还跟着亏几天」。被巡检暂停的目标恢复合格后可自动复跟；
    手动暂停（配置里 paused=true）的目标巡检不碰。
    """

    check_interval_s: float = 21600.0  # 巡检周期；0 或 null 关闭（默认 6 小时）
    auto_pause: bool = True            # 命中排除规则自动暂停（否则只通知）
    auto_resume: bool = True           # 仅对被巡检暂停的目标：恢复合格自动复跟
    discover_interval_s: float = 86400.0  # 候选发现周期：扫全站活跃地址找可跟的新面孔；0/null 关
    discover_candidates: int = 100        # 每轮评估多少个活跃地址

    def __post_init__(self) -> None:
        if self.check_interval_s in (None, 0, 0.0):
            self.check_interval_s = 0.0
        else:
            self.check_interval_s = _positive(
                "health.check_interval_s", self.check_interval_s, allow_none=False
            )
            if self.check_interval_s < 600:
                raise ConfigError(
                    f"health.check_interval_s={self.check_interval_s} 过小："
                    "巡检每目标要拉 2 个接口，过频会撞限流（下限 600）"
                )
        self.auto_pause = bool(self.auto_pause)
        self.auto_resume = bool(self.auto_resume)
        if self.discover_interval_s in (None, 0, 0.0):
            self.discover_interval_s = 0.0
        else:
            self.discover_interval_s = _positive(
                "health.discover_interval_s", self.discover_interval_s, allow_none=False
            )
            if self.discover_interval_s < 3600:
                raise ConfigError(
                    f"health.discover_interval_s={self.discover_interval_s} 过小："
                    "每轮要拉候选数×2 个接口（默认 100 人 ≈ 200 请求），下限 3600"
                )
        self.discover_candidates = max(1, int(self.discover_candidates))


@dataclass
class WatchConfig:
    """信号源（复用 watch 的轮询 + 实时推送）。"""

    stream: bool = True
    poll_interval: float | None = None  # 默认：stream 模式 60s 兜底，纯轮询 10s
    backfill: int = 0

    def __post_init__(self) -> None:
        self.stream = bool(self.stream)
        self.poll_interval = _positive("watch.poll_interval", self.poll_interval)
        self.backfill = max(0, int(self.backfill))


@dataclass
class NotifyConfig:
    """通知渠道；不配 Telegram / Discord 就只打日志。"""

    telegram_bot_token_env: str = ""  # 存 bot token 的环境变量名（不是 token 本身）
    telegram_chat_id: str = ""
    discord_webhook_url_env: str = ""  # 存 Discord 频道 webhook URL 的环境变量名

    def __post_init__(self) -> None:
        self.telegram_bot_token_env = str(self.telegram_bot_token_env or "")
        self.telegram_chat_id = str(self.telegram_chat_id or "")
        self.discord_webhook_url_env = str(self.discord_webhook_url_env or "")


@dataclass
class LiveConfig:
    """实盘（CLOB 下单）参数，mode=paper 时忽略。"""

    private_key_env: str = "POLYCOPYCAT_PRIVATE_KEY"  # 私钥所在环境变量名
    funder: str = ""              # 资金所在地址（proxy wallet）；EOA 直连留空
    signature_type: int = 2       # 0=EOA 1=邮箱钱包代理 2=浏览器钱包代理
    i_understand_live_trading_risk: bool = False  # 必须显式改成 true 才允许实盘

    def __post_init__(self) -> None:
        if self.signature_type not in (0, 1, 2):
            raise ConfigError(f"live.signature_type 只支持 0/1/2，实际是 {self.signature_type!r}")
        self.funder = normalize_address(self.funder) if self.funder else ""
        self.i_understand_live_trading_risk = bool(self.i_understand_live_trading_risk)


@dataclass
class EngineConfig:
    mode: str = "paper"  # paper（纸面模拟）/ live（实盘）
    targets: list[TargetConfig] = field(default_factory=list)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    aggregate: AggregateConfig = field(default_factory=AggregateConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    live: LiveConfig = field(default_factory=LiveConfig)
    ledger_path: str = "data/copycat.sqlite3"
    reconcile_interval_s: float = 300.0
    auto_settle_resolved: bool = True  # 纸面：市场结算后自动按结算价入账
    data_api_url: str | None = None  # 留空用官方入口/环境变量
    ws_url: str | None = None
    clob_url: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in ("paper", "live"):
            raise ConfigError(f"mode 只支持 paper / live，实际是 {self.mode!r}")
        if not self.targets:
            raise ConfigError("targets 至少要配置一个跟单目标地址")
        seen: set[str] = set()
        for target in self.targets:
            if target.address in seen:
                raise ConfigError(f"targets 中地址重复: {target.address}")
            seen.add(target.address)
        self.reconcile_interval_s = _positive(
            "reconcile_interval_s", self.reconcile_interval_s, allow_none=False
        )
        self.auto_settle_resolved = bool(self.auto_settle_resolved)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "EngineConfig":
        if not isinstance(raw, dict):
            raise ConfigError("配置文件顶层应为 JSON 对象")
        data = dict(raw)
        targets_raw = data.pop("targets", [])
        if not isinstance(targets_raw, list):
            raise ConfigError("targets 应为数组")
        try:
            targets = [_build(TargetConfig, t, "targets[]") for t in targets_raw]
        except TypeError as exc:
            raise ConfigError(f"targets 配置有误: {exc}") from exc
        sections = {
            "sizing": SizingConfig, "filters": FilterConfig, "risk": RiskConfig,
            "execution": ExecutionConfig, "aggregate": AggregateConfig,
            "health": HealthConfig, "watch": WatchConfig,
            "notify": NotifyConfig, "live": LiveConfig,
        }
        kwargs: dict[str, Any] = {"targets": targets}
        for name, section_cls in sections.items():
            if name in data:
                kwargs[name] = _build(section_cls, data.pop(name), name)
        known = {f.name for f in fields(cls)}
        unknown = {k for k in data if k not in known and not k.startswith("_")}
        if unknown:
            logger.warning("配置顶层有未知字段将被忽略: %s", ", ".join(sorted(unknown)))
        kwargs.update({k: v for k, v in data.items() if k in known})
        return cls(**kwargs)


def load_config(path: str | Path) -> EngineConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"配置文件不存在: {path}（可从 config.example.json 复制一份改）")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"读取配置 {path} 失败: {exc}") from exc
    try:
        return EngineConfig.from_dict(raw)
    except ConfigError:
        raise
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"配置 {path} 不合法: {exc}") from exc
