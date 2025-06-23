import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Dict, Optional
from datetime import datetime, timedelta
import json
import os
import uvicorn
from auction_agent import AuctionAgent, Product, Bid

app = FastAPI(title="OmniAuction API",
             description="REST API for OmniAuction Voice Agent",
             version="1.0.0")

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# WebSocket manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.agent = AuctionAgent()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except:
                self.active_connections.remove(connection)

manager = ConnectionManager()

# Models
class BidRequest(BaseModel):
    user: str
    amount: float

class AutoBidRequest(BaseModel):
    user: str
    max_bid: float
    product_id: str

class VoiceCommand(BaseModel):
    text: str
    session_id: str

# API Endpoints
@app.get("/api/products", response_model=List[Dict])
async def list_products():
    """Get list of all auction products"""
    products = []
    for product_id, product in manager.agent.products.items():
        products.append({
            "id": product_id,
            "name": product.name,
            "description": product.description,
            "current_highest_bid": product.current_highest_bid,
            "time_remaining": product.time_remaining(),
            "bids_count": len(product.bidding_history),
            "auction_end_time": product.auction_end_time.isoformat()
        })
    return products

@app.get("/api/products/{product_id}", response_model=Dict)
async def get_product(product_id: str):
    """Get details of a specific product"""
    product = manager.agent._find_product(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    return {
        "id": product.id,
        "name": product.name,
        "description": product.description,
        "current_highest_bid": product.current_highest_bid,
        "time_remaining": product.time_remaining(),
        "bids_count": len(product.bidding_history),
        "bidding_history": [
            {"user": bid.user, "amount": bid.amount, "timestamp": bid.timestamp.isoformat()}
            for bid in product.bidding_history[-10:]
        ]
    }

@app.post("/api/bids", status_code=201)
async def place_bid(bid: BidRequest):
    """Place a new bid on a product"""
    product = manager.agent._find_product(bid.product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    # Additional validation for minimum bid amount
    if bid.amount <= product.current_highest_bid:
        raise HTTPException(
            status_code=400, 
            detail=f"Bid must be higher than current highest bid (${product.current_highest_bid:.2f})"
        )
    
    result = manager.agent.place_bid(bid.product_id, bid.amount, bid.user)
    
    if result.startswith("Error"):
        raise HTTPException(status_code=400, detail=result)
    
    # Broadcast the new bid to all connected clients
    await manager.broadcast({
        "type": "bid_placed",
        "product_id": bid.product_id,
        "user": bid.user,
        "amount": bid.amount,
        "current_highest_bid": product.current_highest_bid,
        "message": result
    })
    
    return {
        "status": "success", 
        "message": result,
        "current_highest_bid": product.current_highest_bid
    }

# WebSocket endpoint
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            # Echo back the received message
            await websocket.send_text(f"Message text was: {data}")
    except WebSocketDisconnect:
        manager.disconnect(websocket)

@app.get("/products/{product_id}/bids/count", response_model=Dict[str, int])
async def get_bid_count(product_id: str):
    """Get total number of bids for a product"""
    product = manager.agent.get_product_by_id(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"count": len(product.bidding_history)}

@app.get("/products/{product_id}/bids", response_model=List[Dict])
async def get_bid_history(product_id: str):
    """Get full bid history for a product"""
    product = manager.agent.get_product_by_id(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    return [{
        "user": bid.user,
        "amount": bid.amount,
        "timestamp": bid.timestamp.isoformat()
    } for bid in product.bidding_history]

@app.post("/products/{product_id}/auto-bid", response_model=Dict)
async def set_auto_bid(product_id: str, auto_bid: AutoBidRequest):
    """Set up auto-bidding for a user on a product"""
    product = manager.agent.get_product_by_id(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    # Store auto-bid in the auction agent
    manager.agent.set_auto_bid(product_id, auto_bid.user, auto_bid.max_bid)
    
    return {
        "status": "success",
        "message": f"Auto-bid set for {auto_bid.user} on {product.name} up to ${auto_bid.max_bid}"
    }

# Background task to check for ending auctions
async def check_ending_auctions():
    while True:
        await asyncio.sleep(10)  # Check every 10 seconds
        for product in manager.agent.products.values():
            time_remaining = (product.auction_end_time - datetime.now()).total_seconds()
            if 0 < time_remaining < 60:  # Less than 1 minute remaining
                await manager.broadcast({
                    "type": "auction_ending_soon",
                    "product_id": product.id,
                    "product_name": product.name,
                    "time_remaining": f"{int(time_remaining)} seconds"
                })

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(check_ending_auctions())

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
