"""
Billing subgraph.

Flow: fetch order → generate bill → display total → thank customer.
"""

import logging
from langgraph.graph import StateGraph, START, END
from ..state import AgentState
from ..integrations.pos import POSClient

logger = logging.getLogger("argo_billing")
pos_client = POSClient()


def billing_node(state: AgentState) -> dict:
    order_id = state.get("order_id", "")
    order = state.get("current_order", [])

    if not order and not order_id:
        return {
            "response": "I don't see any active order for your table. Would you like to place an order?",
            "next_action": "",
            "task_status": "idle",
        }

    # Generate bill
    bill = None
    if order_id:
        bill = pos_client.generate_bill(order_id)

    if bill:
        subtotal = bill["subtotal"]
        tax = bill["tax"]
        total = bill["total"]
        items_text = ", ".join(
            f"{i['quantity']} {i['name']}" for i in bill["items"]
        )
        response = (
            f"Your bill for order {order_id}: {items_text}. "
            f"Subtotal ₹{subtotal:.0f}, GST ₹{tax:.0f}, Total ₹{total:.0f}. "
            f"Thank you for dining with us! A waiter will come to collect payment."
        )
        return {
            "bill_generated": True,
            "bill_amount": total,
            "response": response,
            "next_action": "",
            "active_subgraph": "",
            "task_status": "completed",
        }

    # Fallback: calculate from in-memory order
    if order:
        total = sum(i["unit_price"] * i["quantity"] for i in order)
        tax = round(total * 0.05, 2)
        grand = round(total + tax, 2)
        items_text = ", ".join(f"{i['quantity']} {i['name']}" for i in order)
        return {
            "bill_generated": True,
            "bill_amount": grand,
            "response": (
                f"Your bill: {items_text}. "
                f"Subtotal ₹{total:.0f}, GST ₹{tax:.0f}, Total ₹{grand:.0f}. "
                f"Thank you for dining at Argo Kitchen!"
            ),
            "next_action": "",
            "active_subgraph": "",
            "task_status": "completed",
        }

    return {
        "response": "I couldn't fetch your bill. Please call a waiter for assistance.",
        "task_status": "failed",
    }


def build_billing_subgraph() -> StateGraph:
    sg = StateGraph(AgentState)
    sg.add_node("billing", billing_node)
    sg.add_edge(START, "billing")
    sg.add_edge("billing", END)
    return sg.compile()
