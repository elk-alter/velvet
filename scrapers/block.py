# SPDX-License-Identifier: GPL-3.0-only
# Adapted from Amane net/errors.py @ 99369d2; return string codes for Python 3.10 host.
def classify_block(text: str):
    if not text.strip():
        return 'empty_response'
    lower = text.lower()
    if "not available in your region" in lower or "お住まいの地域からはご利用になれません" in text:
        return 'geo_restricted'
    if "banned your access" in lower:
        return 'ip_banned'
    # 挑战页按平台标记识别, 不依赖页面语言 (本地化标题不含 "just a moment"). ``_cf_chl_opt`` 只在
    # 挑战页出现: ``challenge-platform`` 亦是正常页面的 JS Detection 脚本, 不能单独作为判据.
    # 挑战页同样带 ray-id, 因此必须先于封禁页判定.
    if "_cf_chl_opt" in lower:
        return 'cloudflare_challenge'
    if "just a moment" in lower and "cloudflare" in lower:
        return 'cloudflare_challenge'
    if "ray-id" in lower and "cf-" in lower:
        return 'cloudflare_blocked'
    if "driver-verify" in lower:
        return 'age_verification'
    if "年齢認証" in text or "age verification" in lower:
        return 'age_verification'
    return None

