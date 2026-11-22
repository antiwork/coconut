#if os(iOS)
import Intents
import SwiftUI
import UIKit

// The places of Coconut in the row of people at the top of the share sheet of the iPhone, beside the people of
// Messages. iOS suggests the places that the person sends to often, from the sends that the app and its share
// extension tell it about (donate): a project for each message in one of its chats, a team chat or a person for each
// message there. A tap on one opens the share extension with that place chosen (ShareViewController), and a share to a
// project starts a new chat of it, so a link or a screenshot with feedback becomes a task in one tap.
//
// The share extension declares INSendMessageIntent for this (IntentsSupported in project.yml), and the app declares it
// in NSUserActivityTypes, without which iOS refuses each donation. The simulator shows no suggestions; a phone does.

@MainActor
enum ShareSuggestions {
    /// The side of the picture of a place in the row, in points.
    static let side: CGFloat = 60

    /// Tells iOS that the person sent a message to `destination`.
    static func donate(_ destination: ShareDestination) {
        guard !TestIsolation.active else { return }
        let intent = INSendMessageIntent(recipients: nil, outgoingMessageType: .outgoingMessageText, content: nil,
                                         speakableGroupName: INSpeakableString(spokenPhrase: destination.title),
                                         conversationIdentifier: destination.id, serviceName: nil, sender: nil,
                                         attachments: nil)
        if let picture = picture(of: destination) {
            intent.setImage(INImage(imageData: picture), forParameterNamed: \.speakableGroupName)
        }
        let interaction = INInteraction(intent: intent, response: nil)
        interaction.direction = .outgoing
        // The group names the place, so a place that leaves the list of the app leaves the row too (forget).
        interaction.groupIdentifier = destination.id
        interaction.donate { _ in }
    }

    /// Takes the places with these IDs out of the row: they left the list of the app.
    static func forget(_ ids: Set<String>) {
        for id in ids { INInteraction.delete(with: id) { _ in } }
    }

    /// Takes each place out of the row: the app is no longer paired with a Mac.
    static func forgetAll() {
        INInteraction.deleteAll { _ in }
    }

    /// The picture of a place in the row, as a PNG: the face of a team or a person as the app drew it; for a project or
    /// an agent, which have no face in the app, the first letter on the brown of the app icon, so it reads as
    /// Coconut's; else the first letter on a fill of the faces, as the share sheet of Coconut draws a place.
    static func picture(of destination: ShareDestination) -> Data? {
        let view = Group {
            if let face = destination.face, let image = UIImage(data: face) {
                Image(uiImage: image).resizable().interpolation(.high).scaledToFit()
            } else {
                ShareMonogram(destination: destination, size: side,
                              husk: destination.kind == .project || destination.kind == .agent)
            }
        }
        .frame(width: side, height: side)
        .environment(\.colorScheme, .light)
        let renderer = ImageRenderer(content: view)
        renderer.scale = 3
        return renderer.uiImage?.pngData()
    }
}
#endif
