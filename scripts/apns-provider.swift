import Foundation
import CryptoKit

func base64URL(_ data: Data) -> String {
    data.base64EncodedString().replacingOccurrences(of: "+", with: "-")
        .replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
}

func jwt(directory: URL, configuration: [String: String]) throws -> String {
    let keyData = try Data(contentsOf: directory.appendingPathComponent("AuthKey.p8"))
    let identity = SHA256.hash(data: keyData).map { String(format: "%02x", $0) }.joined()
    let cacheURL = directory.appendingPathComponent("token.json")
    let stamp = Int(Date().timeIntervalSince1970)
    if let cacheData = try? Data(contentsOf: cacheURL),
       let cache = try? JSONSerialization.jsonObject(with: cacheData) as? [String: Any],
       cache["identity"] as? String == identity, cache["key_id"] as? String == configuration["key_id"],
       let issued = cache["issued"] as? Int, (0..<2700).contains(stamp - issued),
       let token = cache["token"] as? String { return token }
    let key = try P256.Signing.PrivateKey(pemRepresentation: String(decoding: keyData, as: UTF8.self))
    let header = try JSONSerialization.data(withJSONObject: ["alg": "ES256", "kid": configuration["key_id"]!])
    let claims = try JSONSerialization.data(withJSONObject: ["iss": configuration["team_id"]!, "iat": stamp])
    let input = base64URL(header) + "." + base64URL(claims)
    let signature = try key.signature(for: Data(input.utf8)).rawRepresentation
    let token = input + "." + base64URL(signature)
    let cache = try JSONSerialization.data(withJSONObject: ["identity": identity, "key_id": configuration["key_id"]!, "issued": stamp, "token": token])
    try cache.write(to: cacheURL, options: .atomic)
    try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: cacheURL.path)
    return token
}

@main
struct APNsProvider {
    static func main() async {
        umask(0o077)
        do {
            let directory = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
            let configuration = try JSONDecoder().decode([String: String].self,
                from: Data(contentsOf: directory.appendingPathComponent("config.json")))
            let input = try JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as! [String: Any]
            let environment = input["environment"] as! String
            let host = environment == "development" ? "api.sandbox.push.apple.com" : "api.push.apple.com"
            let device = input["token"] as! String
            guard device.range(of: "^[0-9a-f]{32,512}$", options: .regularExpression) != nil,
                  ["development", "production"].contains(environment),
                  configuration["key_id"] != nil, configuration["team_id"] != nil,
                  let topic = configuration["topic"] else { throw URLError(.badURL) }
            var request = URLRequest(url: URL(string: "https://\(host)/3/device/\(device)")!)
            request.httpMethod = "POST"
            request.httpBody = try JSONSerialization.data(withJSONObject: input["payload"]!)
            request.setValue("bearer " + (try jwt(directory: directory, configuration: configuration)), forHTTPHeaderField: "authorization")
            request.setValue(topic, forHTTPHeaderField: "apns-topic")
            request.setValue("alert", forHTTPHeaderField: "apns-push-type")
            request.setValue("10", forHTTPHeaderField: "apns-priority")
            request.setValue(input["id"] as? String, forHTTPHeaderField: "apns-id")
            request.setValue(input["collapse_id"] as? String, forHTTPHeaderField: "apns-collapse-id")
            request.setValue(String(input["expires"] as! Int), forHTTPHeaderField: "apns-expiration")
            let settings = URLSessionConfiguration.ephemeral
            settings.timeoutIntervalForRequest = 10
            settings.timeoutIntervalForResource = 12
            settings.urlCache = nil
            let session = URLSession(configuration: settings)
            defer { session.invalidateAndCancel() }
            let (data, response) = try await session.data(for: request)
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            let reason = body?["reason"] as? String ?? (status == 200 ? "Accepted" : "Unknown")
            if reason == "ExpiredProviderToken" { try? FileManager.default.removeItem(at: directory.appendingPathComponent("token.json")) }
            let result = try JSONSerialization.data(withJSONObject: ["status": status, "reason": reason])
            FileHandle.standardOutput.write(result)
        } catch {
            FileHandle.standardOutput.write(Data(#"{"status":0,"reason":"TransportUnavailable"}"#.utf8))
        }
    }
}
