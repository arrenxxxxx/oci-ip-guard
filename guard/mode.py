"""Fail-closed mode selection; absent legacy settings never grant DNS writes."""
from dataclasses import dataclass

CONFIRMATION = 'I_HAVE_REVIEWED_DRY_RUN'


@dataclass(frozen=True)
class RunMode:
    name: str
    dry_run: bool
    confirmed: bool
    legacy: bool = False
    error: str = ''


def resolve_mode(env):
    explicit = 'RUN_MODE' in env
    name = env.get('RUN_MODE', '')
    legacy_dry = env.get('DRY_RUN')
    if explicit:
        if name not in ('live', 'simulate'):
            return RunMode('unconfigured', True, False, error='请明确设置 RUN_MODE=live 或 simulate；未选择模式，DNS 写入禁止。')
        if legacy_dry is not None and legacy_dry != ('false' if name == 'live' else 'true'):
            return RunMode(name, True, False, error='RUN_MODE 与 DRY_RUN 冲突；请删除旧 DRY_RUN 或使两者一致。')
    elif legacy_dry in ('true', 'false'):
        name = 'simulate' if legacy_dry == 'true' else 'live'
    elif legacy_dry is None:
        # Old implicit dry-run installs remain non-writing, but cannot look configured.
        return RunMode('unconfigured', True, False, legacy=True,
                       error='未声明运行模式；旧默认观察行为仍禁止写入。请明确选择 RUN_MODE=simulate 或 live。')
    else:
        return RunMode('unconfigured', True, False, legacy=True, error='DRY_RUN 必须为 true 或 false；DNS 写入禁止。')
    confirmed = name == 'live' and env.get('ENABLE_DNS_WRITES') == CONFIRMATION
    if name == 'live' and not confirmed:
        return RunMode(name, True, False, not explicit,
                       '真实模式尚未确认；核对配置后设置 ENABLE_DNS_WRITES=I_HAVE_REVIEWED_DRY_RUN。')
    return RunMode(name, name != 'live', confirmed, not explicit)
