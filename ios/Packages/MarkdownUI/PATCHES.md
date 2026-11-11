# MarkdownUI: the Coconut copy

This folder is a copy of [gonzalezreal/swift-markdown-ui](https://github.com/gonzalezreal/swift-markdown-ui), tag
`2.4.1` (commit `5f613358148239d0292c0cef674a3c2314737f9e`), with one patch. `ios/project.yml` uses it by path for the
iPhone app and the Mac app. The license of the library is in `LICENSE`.

The copy has `Package.swift`, `LICENSE` and `Sources`. It does not have `Tests`, `Examples` and
`Sources/MarkdownUI/Documentation.docc` of the tag.

## The defect

`BlockSequence` puts a gap between two blocks. It gets the margins of the blocks from a preference
(`BlockMarginsPreference`) and keeps them in a state. Until the preference arrives, each gap is the default padding of
SwiftUI. A Mac `List` measures each row when it loads, also the rows off screen, and a row off screen does not get the
preference. So the list measured such a row with the default gaps, 8 points more than the real gap of a paragraph. When
the row came on screen, it got the preference and became shorter, and the chat moved by that difference.

## The patch

The patch gives `BlockSequence` the margins in the same layout pass, as container values. When the preference arrives,
it decides, as before. The picture on screen is the same as without the patch.

| File | Change | Why |
|---|---|---|
| `Package.swift` | The platforms are macOS 15, iOS 18, tvOS 18, Mac Catalyst 18 and watchOS 11. The test target and `swift-snapshot-testing` are removed. | Container values and `Group(subviews:)` need these versions. The copy has no tests. |
| `Sources/MarkdownUI/Views/Blocks/BlockMargin.swift` | `markdownMargin(top:bottom:)` also sets the container value `markdownBlockMargin`. New `ContainerValues` entries `markdownBlockMargin` and `markdownBlockIndex`. | The margin of a block is readable in the layout pass. |
| `Sources/MarkdownUI/Views/Blocks/BlockSequence.swift` | The body reads the margins of its blocks with `Group(subviews:)`. A gap uses the margin from the preference when it has arrived, and the margin from the container value before that. The sequence passes the largest margins of its blocks up as its own container value. | The gaps are correct before the preference arrives, and a list or a quote gets the margins of the blocks in it, as the preference does. |
| `Sources/MarkdownUI/Views/Blocks/ListItemView.swift`, `TaskListItemView.swift` | The label passes the container value of its blocks to the list item. | A `Label` holds its content in a stack, and a stack does not pass container values up. |

Each change has a comment that starts with "Coconut patch".

## Take a newer upstream version

1. Make a diff of this folder against the tag above. It shows the patch: `git diff --no-index <upstream 2.4.1>/Sources Sources`.
2. Replace `Package.swift`, `LICENSE` and `Sources` with the files of the new tag. Remove `Sources/MarkdownUI/Documentation.docc`.
3. Apply the patch again. If upstream fixed the gaps of a row off screen, use the remote package again in
   `ios/project.yml` and remove this folder.
4. Write the new tag and commit in this file.
5. Check that a Mac list measures each kind of Markdown row with the same height at load and on screen, and that the
   pictures of the rows do not change.
