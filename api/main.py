async def process_invoice(invoice: dict, api_key: str, rzp_client) -> dict:
    
    # Stage 1: Detect at-risk (deterministic)
    outstanding = invoice["amount_paise"] - invoice["partial_payment_received_paise"]
    if outstanding <= 0:
        return {"invoice_id": invoice["invoice_id"], "status": "skip_already_settled"}

    # Stage 2: Diagnose (LLM)
    diagnosis = diagnose(invoice, api_key)

    # Stage 3: Build policy input and evaluate
    from policy.engine import PolicyInput, evaluate
    policy_input = PolicyInput(
        invoice_id=invoice["invoice_id"],
        amount_paise=invoice["amount_paise"],
        outstanding_balance_paise=outstanding,
        days_overdue=invoice["days_overdue"],
        account_value_tier=invoice["account_value_tier"],
        evidence_tier=diagnosis["evidence_tier"],
        recommended_action=diagnosis["recommended_action"],
        requested_amount_paise=outstanding,
        existing_payment_link=invoice["payment_link_status"] == "active",
        client_notes=invoice["client_notes"] or "",
        hard_stop=invoice["hard_stop"]
    )
    decision = evaluate(policy_input)

    # Stage 4: Execute if ALLOW
    execution_result = None
    if decision.decision == "ALLOW":
        execution_result = await execute_action(
            action=decision.allowed_actions[0],
            invoice=invoice,
            outstanding_paise=outstanding,
            rzp_client=rzp_client
        )

    # Stage 5: Write audit trail
    audit_entry = {
        **decision.to_audit_entry(),
        "diagnosis": diagnosis,
        "execution": execution_result,
        "outstanding_balance_paise": outstanding
    }
    write_audit(audit_entry)

    return audit_entry