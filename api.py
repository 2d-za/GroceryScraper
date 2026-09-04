"""
Web backend for comparer.py. Run locally with:

    py -m uvicorn api:app --reload

Then either:
  - open http://127.0.0.1:8000/ for the frontend, or
  - POST to /compare directly, e.g.:
        curl -X POST http://127.0.0.1:8000/compare \\
            -H "Content-Type: application/json" \\
            -d '{"product": "Jacobs Gold Instant Coffee 200g", "address": "1 Sandton Drive, Sandton"}'
"""

import json

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from comparer import Offer, compare_product, compare_product_stream

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


def result_to_dict(r: dict) -> dict:
    return {
        "label": r["label"],
        "matches": {retailer: offer_to_dict(o) for retailer, o in r["matches"].items()},
        "cheapest": offer_to_dict(r["cheapest"]),
        "on_deal": [offer_to_dict(o) for o in r["on_deal"]],
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/compare")
async def compare(req: CompareRequest):
    results = await compare_product(req.product, req.address, req.require, req.exclude)
    return {"product": req.product, "results": [result_to_dict(r) for r in results]}


@app.get("/compare/stream")
async def compare_stream(
    product: str,
    address: str,
    require: list[str] | None = Query(None),
    exclude: list[str] = Query([]),
):
    async def events():
        async for event in compare_product_stream(product, address, require, exclude):
            if event["type"] == "result":
                event = {"type": "result", **result_to_dict(event)}
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


# Registered last: an explicit route above always wins for an exact path
# match, so this only ever serves the frontend's static files.
app.mount("/", StaticFiles(directory="static", html=True), name="static")
