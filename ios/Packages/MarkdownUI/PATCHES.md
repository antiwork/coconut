# MarkdownUI: the Coconut copy

This folder is a copy of [gonzalezreal/swift-markdown-ui](https://github.com/gonzalezreal/swift-markdown-ui), tag
`2.4.1` (commit `5f613358148239d0292c0cef674a3c2314737f9e`), with two patches. `ios/project.yml` uses it by path for the
iPhone app and the Mac app. The license of the library is in `LICENSE`.

The copy has `Package.swift`, `LICENSE` and `Sources`. It does not have `Tests`, `Examples` and
`Sources/MarkdownUI/Documentation.docc` of the tag.

## Patch 1: the gaps of a row off screen

### The defect

`BlockSequence` puts a gap between two blocks. It gets the margins of the blocks from a preference
(`BlockMarginsPreference`) and keeps them in a state. Until the preference arrives, each gap is the default padding of
SwiftUI. A Mac `List` measures each row when it loads, also the rows off screen, and a row off screen does not get the
preference. So the list measured such a row with the default gaps, 8 points more than the real gap of a paragraph. When
the row came on screen, it got the preference and became shorter, and the chat moved by that difference.

### The patch

The patch gives `BlockSequence` the margins in the same layout pass, as container values. When the preference arrives,
it decides, as before. The picture on screen is the same as without the patch.

The container margin follows the rule of `BlockMarginsPreference.reduce`: for each edge, the largest value of the block
and of the blocks in it; an unspecified edge does not count (`BlockMargin.merged(with:)`).

| File | Change | Why |
|---|---|---|
| `Package.swift` | The platforms are macOS 15, iOS 18, tvOS 18, Mac Catalyst 18 and watchOS 11. The test target and `swift-snapshot-testing` are removed. | Container values and `Group(subviews:)` need these versions. The copy has no tests. |
| `Sources/MarkdownUI/Views/Blocks/BlockMargin.swift` | `markdownMargin(top:bottom:)` sets the container value `markdownBlockMargin` of the view. New: `ContainerValues` entries `markdownBlockMargin` and `markdownBlockIndex`, `BlockMargin.merged(with:)`, `View.markdownBlockMargin(merging:)` and `SubviewsCollection.markdownBlockMargin`. | The margin of a block is readable in the layout pass, with the same value as the preference. Only a block that holds blocks merges (see below), so a plain block costs one modifier. A container value on a subview does not replace a value that the subview has already, so `markdownBlockMargin(merging:)` holds each subview in a stack and sets the merged value on the stack. |
| `Sources/MarkdownUI/Views/Blocks/BlockSequence.swift` | A sequence of one block shows that block alone: it has no gap, and the margin of the block passes up. A longer sequence reads the margins of its blocks with `Group(subviews:)`. A gap uses the margin from the preference when it has arrived, and the margin from the container value before that. The sequence passes the merged margins of its blocks up as its own container value. | The gaps are correct before the preference arrives. |
| `Sources/MarkdownUI/Views/Blocks/BlockquoteView.swift`, `BulletedListView.swift`, `NumberedListView.swift`, `TaskListView.swift`, `ListItemView.swift`, `TaskListItemView.swift` | The view reads the margins of the blocks in it before the style of the theme, and merges them into the result of the style. Quotes and lists give the style these subviews in a stack, which hides their margins, so the margin of the style is its own. `ListItemView` and `TaskListItemView` make their label from these subviews (`label(_:)`); the `Label` hides their margins. | A style can hold the blocks in a stack, an overlay or a `Label`, and these do not pass container values up. The preference passes them, so the container value must too. |

## Patch 2: HTML tags in a message

Upstream shows raw HTML as text, so a message that ends with `<sub>Records: ...</sub>` showed the tags. The patch
formats the tags that agents write and drops the others. It keeps the text between the tags. It loads nothing: an
`<a>`, an `<img>` and a `<script>` are tags without an effect.

- `<sub>` and `<sup>` after a letter or a digit are a real subscript and superscript (`H<sub>2</sub>O`). At the start
  of a line or a word they are small secondary text, as `<small>` is.
- `<br>` is a line break. `<u>` and `<ins>` underline. `<mark>` has a yellow background. `<kbd>`, `<code>`, `<samp>`
  and `<tt>` use the code style of the theme. `<b>`, `<strong>`, `<i>`, `<em>`, `<s>`, `<del>` and `<strike>` use the
  styles of the theme.
- A tag of another HTML element, and an HTML comment, show nothing.
- A name that is not an HTML element in lowercase is text: `Array<Int>` and `<your-token>` show as written.

| File | Change | Why |
|---|---|---|
| `Sources/MarkdownUI/Parser/HTMLTag.swift` | The pattern is anchored at the start. New: `isElement`, `inlines(inBlock:)` and the public `InlineHTML.plainText(for:)`. | The list of elements decides between a tag and text. The app uses `InlineHTML` for the preview line of a chat. |
| `Sources/MarkdownUI/Renderer/InlineHTMLFormatting.swift` | New. It changes the attributes at an opening tag and puts them back at the closing tag. | Both inline renderers use it. |
| `Sources/MarkdownUI/Renderer/AttributedStringInlineRenderer.swift`, `TextInlineRenderer.swift` | `renderHTML` applies `InlineHTMLFormatting`. The renderers tell it when text, a break or another inline is rendered. | The tags are formatted or dropped. |
| `Sources/MarkdownUI/Parser/MarkdownParser.swift` | An HTML block becomes a paragraph of text and tags. A block of tags alone becomes no block. The text of a `<script>` or a `<style>` block is dropped. | A line that starts with `<div>` or `<details>` is a block for cmark, and it showed raw. An empty block would take a gap. |
| `Sources/MarkdownUI/Utility/InlineNode+PlainText.swift` | The plain text of a tag is empty, or a line break for `<br>`. | The plain text of a heading or a link has no tags. |

Each change of the two patches has a comment that starts with "Coconut patch".

## Take a newer upstream version

1. Make a diff of this folder against the tag above. It shows the patch: `git diff --no-index <upstream 2.4.1>/Sources Sources`.
2. Replace `Package.swift`, `LICENSE` and `Sources` with the files of the new tag. Remove `Sources/MarkdownUI/Documentation.docc`.
3. Apply the patch again. If upstream fixed the gaps of a row off screen, use the remote package again in
   `ios/project.yml` and remove this folder.
4. Write the new tag and commit in this file.
5. Check that a message shows no raw HTML tag: tags in a line, a line of `<sub>` alone, and a `<div>` or a `<details>`
   block.
6. Check that a Mac list measures each kind of Markdown row with the same height at load and on screen, and that the
   pictures of the rows do not change. Include blocks in blocks: a heading in a quote, a quote in a list, a task list.
