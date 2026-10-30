import XCTest
@testable import Coconut

@MainActor
final class AnswerUndoTests: XCTestCase {
    private func receipt(id: String = UUID().uuidString, status: String = "held", seconds: TimeInterval? = 10,
                         undoId: String? = UUID().uuidString, label: String? = "Publish") throws -> Decision {
        var object: [String: Any] = ["id": id, "bot_id": "b", "task_id": "t", "title": "Publish the notes?", "explanation": "",
                                     "action_type": "question", "resource": "", "consequences": "", "recommended": "approve",
                                     "status": status, "urgency": "normal", "created_at": "2026-10-30T12:00:00Z", "revision": 3, "options": []]
        if let seconds {
            object["answer_undo_until"] = ISO8601DateFormatter().string(from: Date().addingTimeInterval(seconds))
        }
        if let undoId { object["answer_undo_id"] = undoId }
        if let label { object["held_answer_label"] = label }
        return try APIJSON.decoder().decode(Decision.self, from: JSONSerialization.data(withJSONObject: object))
    }

    func testAHeldAnswerOffersUndoOnlyInsideItsWindow() throws {
        let held = try receipt()
        XCTAssertTrue(held.isHeld)
        XCTAssertFalse(held.isPending)
        XCTAssertTrue(held.offersAnswerUndo())
        XCTAssertFalse(held.offersAnswerUndo(at: Date().addingTimeInterval(11)))
        XCTAssertEqual(held.heldAnswerLabel, "Publish")
        // A Mac before the Undo window answers at once and sends no deadline, so the app shows no Undo.
        XCTAssertFalse(try receipt(status: "approved", seconds: nil, undoId: nil).offersAnswerUndo())
        XCTAssertFalse(try receipt(seconds: nil).offersAnswerUndo())
        XCTAssertFalse(try receipt(undoId: nil).offersAnswerUndo())
        XCTAssertFalse(try receipt(seconds: -1).offersAnswerUndo())
    }

    func testTheOfferComesFromConfirmedStateAndShowsTheLatestAnswer() throws {
        let store = AppStore(demo: true)
        let first = try receipt(seconds: 5), second = try receipt(seconds: 9)
        // A snapshot or a live update brings held cards, so no receipt callback is needed.
        store.decisions = [first, second, try receipt(status: "approved", seconds: nil, undoId: nil)]
        XCTAssertEqual(store.answerUndoOffer()?.id, second.id)
        XCTAssertEqual(store.answerUndoOffer(at: Date().addingTimeInterval(6))?.id, second.id)
        XCTAssertNil(store.answerUndoOffer(at: Date().addingTimeInterval(11)))
        // An older Mac sends no deadline, so its answered cards offer nothing.
        store.decisions = [try receipt(status: "approved", seconds: nil, undoId: nil)]
        XCTAssertNil(store.answerUndoOffer())
    }

    func testARetriedUndoUsesTheSameRequestKey() {
        let store = AppStore(demo: true)
        let key = store.answerUndoKey(for: "u1")
        XCTAssertEqual(store.answerUndoKey(for: "u1"), key)
        XCTAssertNotEqual(store.answerUndoKey(for: "u2"), key)
    }

    func testUndoSendsTheUndoIdAndTheSameRevision() throws {
        let body = try JSONSerialization.jsonObject(with: APIJSON.encode(
            AnswerUndoRequest(action: "undo_answer", revision: 3, undoId: "u1"))) as? [String: Any]
        XCTAssertEqual(body?["action"] as? String, "undo_answer")
        XCTAssertEqual(body?["revision"] as? Int, 3)
        XCTAssertEqual(body?["undo_id"] as? String, "u1")
    }

    func testUndoOfAnAnswerWithoutAnUndoIdDoesNothing() async throws {
        let store = AppStore(demo: true)
        let undone = await store.undoAnswer(try receipt(undoId: nil))
        XCTAssertFalse(undone)
    }
}
