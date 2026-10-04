package main

import (
	"context"
	"encoding/json"
	"errors"
	"strconv"

	ragv1 "github.com/ggb5023/EcommerceRAG/go/internal/pb/rag/v1"
	"github.com/jackc/pgx/v5"
)

// executionResult is shared by history and turn refresh. Never replace failed
// database reads with empty evidence or infer state from the first execution.
func (g *gateway) executionResult(ctx context.Context, requestID string) (map[string]any, error) {
	var turnID, conversationID int64
	var executionNo int
	var parent *int64
	var state string
	var answer *string
	var mock bool
	err := g.db.QueryRow(ctx, `SELECT t.id,t.conversation_id,e.execution_no,t.parent_turn_id,e.status,e.answer,e.is_mock
		FROM conversation_execution e JOIN turn t ON t.tenant_id=e.tenant_id AND t.id=e.turn_id
		JOIN conversation c ON c.tenant_id=t.tenant_id AND c.id=t.conversation_id
		WHERE e.tenant_id=$1 AND e.request_id=$2 AND c.user_id=$3 AND c.shop_id=$4`,
		g.session.tenantID, requestID, g.session.userID, g.session.shop).
		Scan(&turnID, &conversationID, &executionNo, &parent, &state, &answer, &mock)
	if err != nil {
		return nil, err
	}
	items := make([]map[string]any, 0)
	rows, err := g.db.Query(ctx, `SELECT es.evidence_id,COALESCE(es.document_id,''),COALESCE(es.version_id,''),
		COALESCE(es.source_ref,''),es.content,es.disclosure_class,es.customer_eligible,es.citation_index,COALESCE(es.shop_id,'')
		FROM evidence_snapshot es JOIN conversation_execution e ON e.tenant_id=es.tenant_id AND e.id=es.execution_id
		WHERE e.tenant_id=$1 AND e.request_id=$2 ORDER BY es.citation_index`, g.session.tenantID, requestID)
	if err != nil {
		return nil, err
	}
	for rows.Next() {
		var id, doc, version, ref, content, disclosure, shop string
		var eligible bool
		var index *int
		if err = rows.Scan(&id, &doc, &version, &ref, &content, &disclosure, &eligible, &index, &shop); err != nil {
			break
		}
		var allowed bool
		allowed, err = g.evidenceAllowed(ctx, &ragv1.Evidence{Id: id, DocumentId: doc, VersionId: version, ShopId: shop})
		if err != nil {
			break
		}
		if allowed {
			items = append(items, map[string]any{"id": id, "title": doc, "snippet": content, "sourceType": disclosure,
				"documentId": doc, "versionId": version, "sourceRef": ref, "score": 1.0, "citationIndex": index, "customerEligible": eligible, "shopId": shop})
		}
	}
	rowErr := rows.Err()
	rows.Close()
	if err != nil {
		return nil, err
	}
	if rowErr != nil {
		return nil, rowErr
	}
	citations := make([]map[string]any, 0)
	rows, err = g.db.Query(ctx, `SELECT c.evidence_id,c.citation_index FROM citation c
		JOIN conversation_execution e ON e.tenant_id=c.tenant_id AND e.id=c.execution_id
		WHERE e.tenant_id=$1 AND e.request_id=$2 ORDER BY c.citation_index`, g.session.tenantID, requestID)
	if err != nil {
		return nil, err
	}
	for rows.Next() {
		var id string
		var index int
		if err = rows.Scan(&id, &index); err != nil {
			break
		}
		for _, item := range items {
			if item["id"] == id {
				citations = append(citations, map[string]any{"evidence_id": id, "citation_index": index})
				break
			}
		}
	}
	rowErr = rows.Err()
	rows.Close()
	if err != nil {
		return nil, err
	}
	if rowErr != nil {
		return nil, rowErr
	}
	var reply *string
	var canCopy bool
	blocked := "incomplete"
	err = g.db.QueryRow(ctx, `SELECT text_plain,can_copy,blocked_reason FROM customer_reply cr
		JOIN conversation_execution e ON e.tenant_id=cr.tenant_id AND e.id=cr.execution_id
		WHERE e.tenant_id=$1 AND e.request_id=$2`, g.session.tenantID, requestID).Scan(&reply, &canCopy, &blocked)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}
	if mock {
		canCopy = false
		blocked = "mock"
	}
	if state != "DONE" {
		canCopy = false
	}
	clarification := ""
	if state == "ASKING" {
		var data []byte
		err = g.db.QueryRow(ctx, `SELECT data_json FROM sse_event se JOIN conversation_execution e
			ON e.tenant_id=se.tenant_id AND e.id=se.execution_id WHERE e.tenant_id=$1 AND e.request_id=$2
			AND data_json->>'status'='ASKING' ORDER BY sequence DESC LIMIT 1`, g.session.tenantID, requestID).Scan(&data)
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return nil, err
		}
		if len(data) > 0 {
			var object struct {
				Clarification string `json:"clarification"`
			}
			if err = json.Unmarshal(data, &object); err != nil {
				return nil, err
			}
			clarification = object.Clarification
		}
	}
	var parentID any
	if parent != nil {
		parentID = strconv.FormatInt(*parent, 10)
	}
	return map[string]any{"conversation_id": strconv.FormatInt(conversationID, 10), "turn_id": strconv.FormatInt(turnID, 10),
		"execution_no": executionNo, "request_id": requestID, "status": state, "answer": answer, "is_mock": mock,
		"evidence": items, "citations": citations, "parent_turn_id": parentID, "clarification": clarification,
		"events_url": "/v1/turns/" + requestID + "/events", "customer_reply": map[string]any{"text_plain": reply, "can_copy": canCopy, "blocked_reason": blocked}}, nil
}
