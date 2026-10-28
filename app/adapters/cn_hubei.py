"""Hubei admissions/adjustments; pricing withheld until its convention is reviewed."""

from app.adapters._cn_provincial import CnProvincialAdapter
from app.adapters._cn_public import PublicChinaMixin


class CnHubeiAdapter(PublicChinaMixin, CnProvincialAdapter):
    adapter_id = "cn_hubei"
    host = "ybj.hubei.gov.cn"
    province = "CN-HB"
    limitations = (
        "Internal research; reuse rights must be reviewed. Complete public archive enumeration; unavailable pages "
        "and unsupported documents explicitly reported. All extractable price cells exported; unverified units "
        "are not normalized. No bypass of HTTP 412, authentication or CA certificates."
    )
    default_documents = [
        {
            "url": "https://ybj.hubei.gov.cn/zfxxgk/zc/qtzdgkwj/202610/P020261014552464734543.xlsx",
            "notice_url": "https://ybj.hubei.gov.cn/zfxxgk/zc/qtzdgkwj/202610/t20261014_6032706.shtml",
            "kind": "hb_admission", "status": "final", "published_at": "2026-10-12", "effective_from": "2026-10-12",
        },
        {
            "url": "https://ybj.hubei.gov.cn/zfxxgk/zc/qtzdgkwj/202610/P020261014553630102139.xlsx",
            "notice_url": "https://ybj.hubei.gov.cn/zfxxgk/zc/qtzdgkwj/202610/t20261014_6032710.shtml",
            "kind": "hb_adjustment", "status": "final", "published_at": "2026-10-13", "effective_from": "2026-10-13",
        },
        {
            "url": "https://ybj.hubei.gov.cn/zfxxgk/zc/qtzdgkwj/tzgg_2022/202610/P020261016602349093795.xlsx",
            "notice_url": "https://ybj.hubei.gov.cn/zfxxgk/zc/qtzdgkwj/tzgg_2022/202610/t20261016_6034444.shtml",
            "kind": "hb_award_adjustment", "status": "final", "published_at": "2026-10-16",
        },
    ]
