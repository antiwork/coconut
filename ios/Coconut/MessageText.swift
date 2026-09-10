import SwiftUI
import MarkdownUI

/// Keep message formatting separate from delivery and conversation state.
struct MessageText: View {
    let text: String
    var linkColor: Color = .blue

    var body: some View {
        Markdown(text.trimmingCharacters(in: .newlines))
            .font(.body)
            .markdownTheme(.coconutMessage)
            .textSelection(.enabled)
            .tint(linkColor)
            .accessibilityElement(children: .combine)
    }
}

@MainActor
private extension Theme {
    static let coconutMessage = Theme.basic
        .heading1 { configuration in
            configuration.label
                .markdownTextStyle { FontWeight(.semibold); FontSize(.em(1.25)) }
                .markdownMargin(top: .em(0.8), bottom: .em(0.35))
        }
        .heading2 { configuration in
            configuration.label
                .markdownTextStyle { FontWeight(.semibold); FontSize(.em(1.15)) }
                .markdownMargin(top: .em(0.8), bottom: .em(0.35))
        }
        .heading3 { configuration in
            configuration.label
                .markdownTextStyle { FontWeight(.semibold); FontSize(.em(1)) }
                .markdownMargin(top: .em(0.6), bottom: .em(0.3))
        }
        .paragraph { configuration in
            configuration.label
                .fixedSize(horizontal: false, vertical: true)
                .relativeLineSpacing(.em(0.08))
                .markdownMargin(top: .zero, bottom: .em(0.6))
        }
        .blockquote { configuration in
            configuration.label
                .foregroundStyle(.secondary)
                .padding(.leading, 12)
                .overlay(alignment: .leading) { Rectangle().fill(.quaternary).frame(width: 3) }
                .markdownMargin(top: .em(0.3), bottom: .em(0.6))
        }
        .codeBlock { configuration in
            ScrollView(.horizontal) {
                configuration.label
                    .fixedSize(horizontal: true, vertical: true)
                    .markdownTextStyle { FontFamilyVariant(.monospaced); FontSize(.em(0.85)) }
                    .padding(10)
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(configuration.content)
            .accessibilityAddTraits(.isStaticText)
            .background(.primary.opacity(0.045), in: RoundedRectangle(cornerRadius: 10))
            .markdownMargin(top: .em(0.3), bottom: .em(0.6))
        }
}
