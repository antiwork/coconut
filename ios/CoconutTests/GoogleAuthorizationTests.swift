import XCTest
@testable import Coconut

final class GoogleAuthorizationTests: XCTestCase {
    let authorization = GoogleAuthorization(authorizationUrl: "https://accounts.google.com/o/oauth2/v2/auth", callbackScheme: "com.googleusercontent.apps.example", state: "expected")

    func testReturnsCodeOnlyForMatchingCallback() throws {
        let url = URL(string: "com.googleusercontent.apps.example:/oauthredirect?state=expected&code=accepted")!
        XCTAssertEqual(try authorization.callbackCode(url), "accepted")
    }

    func testRejectsWrongSchemePathStateAndDuplicateFields() {
        for value in [
            "other:/oauthredirect?state=expected&code=code",
            "com.googleusercontent.apps.example:/wrong?state=expected&code=code",
            "com.googleusercontent.apps.example:/oauthredirect?state=other&code=code",
            "com.googleusercontent.apps.example:/oauthredirect?state=expected&state=other&code=code",
            "com.googleusercontent.apps.example:/oauthredirect?state=expected&code=a&code=b",
            "com.googleusercontent.apps.example:/oauthredirect?state=expected&error=access_denied",
            "com.googleusercontent.apps.example:/oauthredirect?state=expected"
        ] {
            XCTAssertThrowsError(try authorization.callbackCode(URL(string: value)!))
        }
    }
}
