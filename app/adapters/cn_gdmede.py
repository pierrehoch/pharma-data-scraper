"""Bounded public Guangdong pack-listing disclosures, opt-in internal research."""

from app.adapters._cn_provincial import CnProvincialAdapter
from app.adapters._cn_public import PublicChinaMixin


class CnGdmedeAdapter(PublicChinaMixin, CnProvincialAdapter):
    adapter_id = "cn_gdmede"
    host = "www.gdmede.com.cn"
    province = "CN-GD"
    limitations = (
        "Internal research; commercial reuse requires authorization. Full public drug-notice catalogue and paginated "
        "procurement query. Original price cells are exported even when units/status prevent normalization. "
        "Not exhaustive/current market coverage or actual paid transactions; exclusions are audited."
    )
    default_documents = [
        {
            "url": "https://www.gdmede.com.cn/attachement/2109895084907433984.xlsx",
            "notice_url": "https://www.gdmede.com.cn/announcement/announcement/detail?id=2109895026111680512",
            "kind": "gd_listing", "status": "final", "published_at": "2026-10-13",
        },
        {
            "url": "https://www.gdmede.com.cn/attachement/2111020161736642560.xlsx",
            "notice_url": "https://www.gdmede.com.cn/announcement/announcement/detail?id=2111020185140858880",
            "kind": "gd_listing", "status": "final", "published_at": "2026-10-16",
        },
        {
            "url": "https://www.gdmede.com.cn/attachement/2113521357936398336.xlsx",
            "notice_url": "https://www.gdmede.com.cn/announcement/announcement/detail?id=2113521363808423936",
            "kind": "gd_listing", "status": "final", "published_at": "2026-10-23",
        },
    ]
