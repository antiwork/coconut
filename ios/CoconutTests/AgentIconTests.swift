import XCTest
@testable import Coconut

/// A generated icon of an agent comes from the Mac as `avatar_image`, and the avatar loads it as a Mac file.
final class AgentIconTests: XCTestCase {
    func testTheAvatarReadsTheIconOfTheAgent() throws {
        let json = """
        {"id": "b", "session_id": "s", "name": "Support", "role": "", "avatar": "coconut.round", "color": "A85F32",
         "instructions": "", "status": "idle", "last_message": "", "unread_count": 0, "archived": false,
         "created_at": "2026-11-16T00:00:00Z", "avatar_image": "1f0c9a2e-0000-4000-8000-000000000001"}
        """
        let bot = try APIJSON.decoder().decode(Bot.self, from: Data(json.utf8))
        XCTAssertEqual(bot.avatarImage, "1f0c9a2e-0000-4000-8000-000000000001")
        let avatar = BotAvatar(bot: bot, size: 20)
        XCTAssertEqual(avatar.image?.absoluteString, "coconut-file://1f0c9a2e-0000-4000-8000-000000000001")
        XCTAssertTrue(PhotoLoader.isMacFile(try XCTUnwrap(avatar.image)))
        var plain = bot
        plain.avatarImage = nil
        XCTAssertNil(BotAvatar(bot: plain).image)
    }
}
