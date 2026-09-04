"""
Web backend for comparer.py. Run locally with:

    py -m uvicorn api:app --reload

Then POST to /compare, e.g.:

    curl -X POST http://127.0.0.1:8000/compare \\
        -H "Content-Type: application/json" \\
        -d '{"product": "Jacobs Gold Instant Coffee 200g", "address": "1 Sandton Drive, Sandton"}'
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict

from comparer import Offer, compare_product

app = FastAPI(title="GroceryScraper API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class CompareRequest(BaseModel):
    product: str
    address: str
    require: list[str] | None = None
    exclude: list[str] = []

    # Without this, Swagger's "Try it out" auto-fills require/exclude with a
    # generic ["string"] placeholder — if left in, every offer needs the
    # literal word "string" in its name, so nothing ever matches.
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "product": "Jacobs Gold Instant Coffee 200g",
            "address": "1 Sandton Drive, Sandton",
            "require": None,
            "exclude": [],
        }
    })


def offer_to_dict(offer: Offer | None) -> dict | None:
    if offer is None:
        return None
    return {
        "retailer": offer.retailer,
        "name": offer.name,
        "price": offer.price,
        "deal_label": offer.deal_label,
        "url": offer.url,
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/compare")
async def compare(req: CompareRequest):
    results = await compare_product(req.product, req.address, req.require, req.exclude)
    return {
        "product": req.product,
        "results": [
            {
                "label": r["label"],
                "matches": {retailer: offer_to_dict(o) for retailer, o in r["matches"].items()},
                "cheapest": offer_to_dict(r["cheapest"]),
                "on_deal": [offer_to_dict(o) for o in r["on_deal"]],
            }
            for r in results
        ],
    }
