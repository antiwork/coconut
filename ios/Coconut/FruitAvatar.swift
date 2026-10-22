import SwiftUI

// Vector paths use the SVG coordinate system and compile once for native rendering.
struct FruitArtwork: Decodable {
    struct Layer: Decodable { let fill: String; let d: String }
    let name: String
    let id: String
    let color: String
    let face: [Double]
    let ink: String
    let layers: [Layer]
    static let all: [Self] = try! JSONDecoder().decode([Self].self, from: Data(source.utf8))
    static let paths: [[Path]] = all.map { $0.layers.map { path($0.d) } }
    private static let source = #"""
[{"name":"Coconut","id":"coconut","color":"A86640","face":[51,55,0.96],"ink":"263329","layers":[{"fill":"A86640","d":"M 17 48 C 20 25 40 15 62 24 C 82 30 91 48 85 66 C 79 84 58 91 38 84 C 19 78 12 64 17 48 Z"},{"fill":"F5EBCF","d":"M 22 47 C 25 29 43 23 60 29 C 76 34 82 48 77 63 C 71 78 52 82 37 76 C 23 70 17 60 22 47 Z"}]},{"name":"Apple","id":"apple","color":"F06C63","face":[50,57,1],"ink":"263329","layers":[{"fill":"F06C63","d":"M 50 30 C 36 18 18 26 17 46 C 15 62 27 85 40 86 C 45 86 48 82 51 82 C 57 82 60 88 68 82 C 79 72 88 50 80 36 C 73 23 61 23 50 30 Z"},{"fill":"775139","d":"M 47 28 C 45 20 47 15 50 12 L 54 14 C 50 19 51 23 52 27 Z"},{"fill":"7EB96A","d":"M 50 25 C 51 12 64 10 74 13 C 70 24 60 28 50 25 Z"}]},{"name":"Pear","id":"pear","color":"BDCD67","face":[50,63,1],"ink":"263329","layers":[{"fill":"BDCD67","d":"M 50 21 C 37 21 40 36 28 49 C 9 69 25 88 50 88 C 77 88 91 70 75 50 C 62 35 63 21 50 21 Z"},{"fill":"7C5B3B","d":"M 48 23 C 45 14 50 8 55 9 L 57 13 C 51 14 52 19 52 23 Z"},{"fill":"719956","d":"M 54 21 C 60 10 72 15 76 20 C 68 27 59 27 54 21 Z"}]},{"name":"Strawberry","id":"strawberry","color":"EE6B7A","face":[50,51,0.88],"ink":"263329","layers":[{"fill":"EE6B7A","d":"M 20 35 C 22 20 40 24 50 28 C 65 19 82 26 82 41 C 82 57 62 84 51 88 C 39 84 17 57 20 35 Z"},{"fill":"69A977","d":"M 49 30 L 30 29 L 35 19 L 44 23 L 50 13 L 56 23 L 67 18 L 70 30 L 56 29 L 51 40 Z"},{"fill":"F9C394","d":"M 28 43 L 30 48 L 26 47 Z M 70 42 L 73 47 L 69 48 Z M 35 65 L 38 69 L 34 70 Z M 65 65 L 66 70 L 62 70 Z M 49 77 L 51 81 L 47 80 Z"}]},{"name":"Lemon","id":"lemon","color":"F1D46B","face":[53,54,0.94],"ink":"263329","layers":[{"fill":"F1D46B","d":"M 16 57 C 16 50 23 52 25 43 C 31 24 54 18 72 29 C 82 36 78 44 86 45 C 94 47 86 54 83 57 C 76 82 45 86 28 71 C 21 65 16 64 16 57 Z"},{"fill":"8CAA65","d":"M 41 26 C 41 13 54 10 64 16 C 60 26 51 29 41 26 Z"}]},{"name":"Orange","id":"orange","color":"EAA052","face":[50,57,1],"ink":"263329","layers":[{"fill":"EAA052","d":"M 16 56 C 16 37.775 31.222 23 50 23 C 68.778 23 84 37.775 84 56 C 84 74.225 68.778 89 50 89 C 31.222 89 16 74.225 16 56 Z"},{"fill":"72A779","d":"M 49 27 C 52 13 68 12 76 18 C 70 29 59 32 49 27 Z"},{"fill":"967348","d":"M 46 28 L 47 18 L 51 18 L 52 28 Z"}]},{"name":"Peach","id":"peach","color":"F2A088","face":[48,55,0.98],"ink":"263329","layers":[{"fill":"F2A088","d":"M 51 31 C 32 18 16 32 18 52 C 19 72 42 86 51 88 C 62 83 81 71 82 51 C 84 31 65 22 51 31 Z"},{"fill":"DE8377","d":"M 55 33 C 66 50 64 70 52 85 C 60 65 60 46 51 33 Z"},{"fill":"7EAF81","d":"M 51 31 C 54 14 70 13 77 19 C 74 31 62 35 51 31 Z"}]},{"name":"Cherry","id":"cherry","color":"D96377","face":[48,62,0.92],"ink":"263329","layers":[{"fill":"D96377","d":"M 18 61 C 18 45.536 31.431 33 48 33 C 64.569 33 78 45.536 78 61 C 78 76.464 64.569 89 48 89 C 31.431 89 18 76.464 18 61 Z"},{"fill":"769561","d":"M 48 37 C 53 26 61 14 67 8 L 71 10 C 63 23 56 30 52 38 Z"},{"fill":"91B075","d":"M 64 18 C 49 7 33 13 32 22 C 44 28 56 28 64 18 Z"}]},{"name":"Plum","id":"plum","color":"AA79C2","face":[50,57,0.99],"ink":"FCF0DD","layers":[{"fill":"AA79C2","d":"M 49 27 C 30 20 19 35 22 57 C 25 78 39 89 54 87 C 74 85 84 66 80 46 C 77 29 62 24 49 27 Z"},{"fill":"739B79","d":"M 50 25 C 51 12 64 10 74 13 C 70 24 60 28 50 25 Z"},{"fill":"8B65A0","d":"M 57 29 C 68 44 71 65 58 84 C 65 61 61 42 53 29 Z"}]},{"name":"Blueberry","id":"blueberry","color":"7E99D3","face":[50,61,0.95],"ink":"1F334E","layers":[{"fill":"7E99D3","d":"M 16 56 C 16 37.775 31.222 23 50 23 C 68.778 23 84 37.775 84 56 C 84 74.225 68.778 89 50 89 C 31.222 89 16 74.225 16 56 Z"},{"fill":"536EA6","d":"M 34 25 L 46 28 L 51 19 L 57 28 L 69 26 L 63 35 L 67 44 L 54 39 L 45 44 L 44 35 Z"}]},{"name":"Watermelon","id":"watermelon","color":"79AC84","face":[50,49,1.06],"ink":"263329","layers":[{"fill":"79AC84","d":"M 9 23 L 91 23 C 84 60 68 85 50 87 C 30 84 15 60 9 23 Z"},{"fill":"F0DDAA","d":"M 14 23 L 86 23 C 79 58 65 78 50 80 C 34 78 21 58 14 23 Z"},{"fill":"ED8D87","d":"M 19 23 L 81 23 C 74 56 63 72 50 74 C 37 72 26 56 19 23 Z"},{"fill":"764E49","d":"M 27 30 L 30 36 L 26 35 Z M 73 30 L 74 35 L 70 36 Z"}]},{"name":"Pineapple","id":"pineapple","color":"DAB26C","face":[51,64,0.94],"ink":"263329","layers":[{"fill":"DAB26C","d":"M 32 39 C 13 53 20 83 44 89 C 66 94 83 80 81 61 C 80 42 67 34 50 37 Z"},{"fill":"77A57C","d":"M 40 41 L 27 24 L 41 28 L 43 10 L 52 25 L 63 10 L 63 30 L 79 24 L 65 43 Z"},{"fill":"C39252","d":"M 27 52 L 32 56 L 27 60 L 22 56 Z M 70 52 L 75 56 L 70 60 L 65 56 Z M 34 76 L 39 80 L 35 84 L 30 80 Z M 66 76 L 71 80 L 67 84 L 62 80 Z"}]},{"name":"Banana","id":"banana","color":"EBC96C","face":[47,66,1],"ink":"263329","layers":[{"fill":"EBC96C","d":"M 25 15 C 19 42 41 54 66 43 C 75 39 80 31 83 24 C 93 51 74 84 48 87 C 21 90 9 62 14 39 C 17 27 22 20 25 15 Z"},{"fill":"A47A45","d":"M 21 22 L 22 12 L 28 13 L 27 22 Z M 80 27 L 82 19 L 87 21 L 86 29 Z"},{"fill":"D2A851","d":"M 20 37 C 17 54 21 69 31 77 C 20 73 14 56 20 37 Z"}]},{"name":"Avocado","id":"avocado","color":"70A779","face":[50,61,0.83],"ink":"FCEDCE","layers":[{"fill":"70A779","d":"M 50 14 C 35 14 37 34 23 50 C 9 69 23 88 50 89 C 79 89 92 69 77 50 C 64 34 66 14 50 14 Z"},{"fill":"D8D68D","d":"M 50 21 C 41 21 41 39 30 53 C 19 68 29 82 50 82 C 72 82 81 68 70 53 C 59 39 59 21 50 21 Z"},{"fill":"AD7954","d":"M 31 61 C 31 50.507 39.507 42 50 42 C 60.493 42 69 50.507 69 61 C 69 71.493 60.493 80 50 80 C 39.507 80 31 71.493 31 61 Z"}]},{"name":"Fig","id":"fig","color":"AE79AC","face":[49,62,1],"ink":"FCF0DD","layers":[{"fill":"AE79AC","d":"M 50 14 C 42 14 42 34 28 47 C 11 65 22 84 45 89 C 69 94 87 78 79 58 C 71 43 57 38 56 20 Z"},{"fill":"7EA383","d":"M 50 21 C 45 13 48 8 53 7 L 58 10 L 56 23 Z"},{"fill":"8C608E","d":"M 57 31 C 68 50 69 70 55 87 C 64 68 61 50 53 32 Z"}]},{"name":"Mango","id":"mango","color":"EDB76C","face":[48,54,1],"ink":"263329","layers":[{"fill":"EDB76C","d":"M 68 20 C 42 13 21 31 19 55 C 16 76 32 88 52 84 C 66 82 64 67 77 54 C 90 39 85 24 68 20 Z"},{"fill":"E99A65","d":"M 71 25 C 84 31 80 45 68 55 C 59 65 61 79 49 84 C 68 86 67 68 79 53 C 90 37 83 25 71 25 Z"},{"fill":"79A780","d":"M 61 22 C 61 9 70 8 81 12 C 79 23 69 28 61 22 Z"}]},{"name":"Kiwi","id":"kiwi","color":"A68456","face":[50,54,1.03],"ink":"263329","layers":[{"fill":"A68456","d":"M 15 54 C 15 34.118 30.67 18 50 18 C 69.33 18 85 34.118 85 54 C 85 73.882 69.33 90 50 90 C 30.67 90 15 73.882 15 54 Z"},{"fill":"AFC777","d":"M 21 54 C 21 37.431 33.984 24 50 24 C 66.016 24 79 37.431 79 54 C 79 70.569 66.016 84 50 84 C 33.984 84 21 70.569 21 54 Z"},{"fill":"DCE3A9","d":"M 29 54 C 29 41.85 38.402 32 50 32 C 61.598 32 71 41.85 71 54 C 71 66.15 61.598 76 50 76 C 38.402 76 29 66.15 29 54 Z"},{"fill":"50653F","d":"M 29 38 L 33 41 L 30 43 Z M 68 37 L 70 42 L 66 41 Z M 24 57 L 29 56 L 28 60 Z M 74 57 L 72 61 L 70 57 Z M 37 76 L 40 72 L 42 76 Z M 59 75 L 63 73 L 62 78 Z"}]},{"name":"Grapefruit","id":"grapefruit","color":"EEC77F","face":[50,54,1.04],"ink":"263329","layers":[{"fill":"EEC77F","d":"M 15 54 C 15 34.67 30.67 19 50 19 C 69.33 19 85 34.67 85 54 C 85 73.33 69.33 89 50 89 C 30.67 89 15 73.33 15 54 Z"},{"fill":"FAE5B6","d":"M 20 54 C 20 37.431 33.431 24 50 24 C 66.569 24 80 37.431 80 54 C 80 70.569 66.569 84 50 84 C 33.431 84 20 70.569 20 54 Z"},{"fill":"E98079","d":"M 25 54 C 25 40.193 36.193 29 50 29 C 63.807 29 75 40.193 75 54 C 75 67.807 63.807 79 50 79 C 36.193 79 25 67.807 25 54 Z"},{"fill":"EFC29C","d":"M 48 30 L 51 30 L 51 37 L 49 37 Z M 72 48 L 76 47 L 77 50 L 72 51 Z M 29 62 L 24 65 L 25 68 L 31 65 Z M 53 71 L 55 71 L 58 78 L 55 79 Z"}]},{"name":"Cantaloupe","id":"cantaloupe","color":"B6C493","face":[50,55,1],"ink":"263329","layers":[{"fill":"B6C493","d":"M 15 55 C 15 36.222 30.67 21 50 21 C 69.33 21 85 36.222 85 55 C 85 73.778 69.33 89 50 89 C 30.67 89 15 73.778 15 55 Z"},{"fill":"E3CEA0","d":"M 20 55 C 20 38.984 33.431 26 50 26 C 66.569 26 80 38.984 80 55 C 80 71.016 66.569 84 50 84 C 33.431 84 20 71.016 20 55 Z"},{"fill":"F0AE71","d":"M 24 55 C 24 41.193 35.641 30 50 30 C 64.359 30 76 41.193 76 55 C 76 68.807 64.359 80 50 80 C 35.641 80 24 68.807 24 55 Z"},{"fill":"799770","d":"M 50 25 C 51 12 64 10 74 13 C 70 24 60 28 50 25 Z"}]},{"name":"Raspberry","id":"raspberry","color":"D76C91","face":[50,56,1],"ink":"FFEBD6","layers":[{"fill":"D76C91","d":"M 50 22 C 38 17 28 24 29 32 C 17 30 12 42 18 51 C 8 61 18 73 28 73 C 27 85 41 93 50 85 C 61 95 75 86 74 75 C 87 75 94 61 83 53 C 91 41 81 29 72 32 C 70 20 58 17 50 22 Z"},{"fill":"66986D","d":"M 32 29 L 39 17 L 50 24 L 60 15 L 68 28 L 51 35 Z"},{"fill":"B45078","d":"M 19 51 Q 25 47 29 51 L 28 54 Q 24 51 20 54 Z M 72 72 Q 72 68 77 67 L 79 70 Q 75 70 75 74 Z"}]},{"name":"Grapes","id":"grapes","color":"9675B9","face":[50,53,1.04],"ink":"FFEED7","layers":[{"fill":"9675B9","d":"M 48 26 C 34 18 22 29 25 40 C 11 44 13 62 26 66 C 24 80 39 87 49 80 C 61 88 77 79 74 67 C 89 60 85 43 74 40 C 77 25 62 20 48 26 Z"},{"fill":"725395","d":"M 26 40 C 29 43 31 47 30 51 L 27 51 C 28 47 26 44 24 43 Z M 70 60 L 73 62 C 70 69 66 72 62 72 L 61 69 C 65 69 69 65 70 60 Z"},{"fill":"729B73","d":"M 48 28 C 46 14 57 7 66 12 C 63 26 56 31 48 28 Z"}]},{"name":"Papaya","id":"papaya","color":"88A573","face":[49,53,1],"ink":"FFEDD3","layers":[{"fill":"88A573","d":"M 48 14 C 27 18 15 40 20 65 C 25 87 49 94 60 84 C 80 71 90 43 76 20 C 69 11 57 9 48 14 Z"},{"fill":"EBA169","d":"M 48 21 C 32 24 23 42 27 62 C 31 80 49 86 60 76 C 74 65 80 42 70 25 C 60 17 55 18 48 21 Z"},{"fill":"755745","d":"M 29 54 C 29 40.745 37.954 30 49 30 C 60.046 30 69 40.745 69 54 C 69 67.255 60.046 78 49 78 C 37.954 78 29 67.255 29 54 Z"}]},{"name":"Dragonfruit","id":"dragonfruit","color":"DC7797","face":[50,54,1],"ink":"263329","layers":[{"fill":"DC7797","d":"M 27 30 L 19 19 L 36 22 C 43 17 50 17 56 20 L 70 12 L 69 27 C 80 36 80 43 81 49 L 91 54 L 80 62 C 78 80 63 89 49 86 L 39 94 L 35 82 C 19 75 18 61 19 50 L 9 42 L 22 41 Z"},{"fill":"F4E2D4","d":"M 24 54 C 24 37.984 35.641 25 50 25 C 64.359 25 76 37.984 76 54 C 76 70.016 64.359 83 50 83 C 35.641 83 24 70.016 24 54 Z"},{"fill":"855A70","d":"M 33 35 L 36 39 L 32 39 Z M 65 34 L 67 38 L 63 38 Z M 29 59 L 32 62 L 28 63 Z M 70 60 L 70 64 L 67 62 Z M 40 70 L 42 74 L 38 74 Z M 60 73 L 60 77 L 57 75 Z"}]},{"name":"Starfruit","id":"starfruit","color":"D9C45D","face":[50,54,1],"ink":"263329","layers":[{"fill":"D9C45D","d":"M 50 10 C 54 10 60 31 63 34 C 69 35 90 35 91 40 C 93 45 70 58 69 62 C 70 68 79 87 75 90 C 71 93 54 79 50 78 C 45 79 29 93 25 90 C 21 87 30 67 31 62 C 29 57 8 46 9 41 C 10 36 30 36 36 34 C 40 30 45 10 50 10 Z"},{"fill":"F1D978","d":"M 50 22 L 60 40 L 77 43 L 62 56 L 60 75 L 50 68 L 38 75 L 40 56 L 23 43 L 41 41 Z"}]}]
"""#

    private static func path(_ source: String) -> Path {
        let tokens = source.split(separator: " ").map(String.init)
        var index = 0
        func number() -> Double { defer { index += 1 }; return Double(tokens[index])! }
        func point() -> CGPoint { CGPoint(x: number(), y: number()) }
        return Path { path in
            while index < tokens.count {
                let command = tokens[index]; index += 1
                switch command {
                case "M": path.move(to: point())
                case "L": path.addLine(to: point())
                case "C":
                    let a = point(), b = point(), end = point()
                    path.addCurve(to: end, control1: a, control2: b)
                case "Q":
                    let control = point(), end = point()
                    path.addQuadCurve(to: end, control: control)
                case "Z": path.closeSubpath()
                default: preconditionFailure("Invalid fruit path")
                }
            }
        }
    }

    // Freeze fallback assignments when later releases add or reorder fruits.
    private static let originalIDs = ["coconut", "apple", "pear", "strawberry", "lemon", "orange", "peach", "cherry", "plum", "blueberry", "watermelon", "pineapple", "banana", "avocado", "fig", "mango", "kiwi", "grapefruit", "cantaloupe", "raspberry", "grapes", "papaya", "dragonfruit", "starfruit"]

    static func index(symbol: String, identity: String) -> Int {
        if let exact = all.firstIndex(where: { "fruit." + $0.id == symbol }) { return exact }
        let id = originalIDs[Int(characterSeed(identity.isEmpty ? symbol : identity) % UInt64(originalIDs.count))]
        return all.firstIndex { $0.id == id }!
    }
}

struct FruitFigure: View, Animatable {
    // Radius of the face sphere and the resting positions of its features, in artwork units.
    static let faceRadius = 22.0
    static let eyeLongitude = 0.42
    static let eyeLatitude = -0.32
    static let mouthLatitude = 0.34
    let index: Int
    let size: CGFloat
    let seed: UInt64
    let time: Double
    var thinking: Double
    var eyelid: Double
    var gesture: CoconutGesture = .ponder
    var previousGesture: CoconutGesture? = nil
    var gestureChangedAt = -Double.infinity
    nonisolated var animatableData: AnimatablePair<Double, Double> {
        get { AnimatablePair(thinking, eyelid) }
        set { thinking = newValue.first; eyelid = newValue.second }
    }
    var body: some View {
        let artwork = FruitArtwork.all[index]
        let expression = CoconutExpression.sample(time: time, seed: seed, thinking: thinking, gesture: gesture,
                                                  previous: previousGesture, changedAt: gestureChangedAt)
        let radius = Self.faceRadius
        Canvas { context, _ in
            context.scaleBy(x: size / 100, y: size / 100)
            let paths = FruitArtwork.paths[index]
            for (layer, path) in zip(artwork.layers, paths) {
                context.fill(path, with: .color(Color(hex: layer.fill)))
            }
            // Features that turn past the edge of the fruit disappear behind it.
            context.clip(to: paths[0])
            context.translateBy(x: artwork.face[0], y: artwork.face[1])
            context.scaleBy(x: artwork.face[2], y: artwork.face[2])
            let ink = Color(hex: artwork.ink)
            let latitude = Self.eyeLatitude + expression.pitch
            for side in [-1.0, 1.0] {
                let longitude = expression.yaw + side * Self.eyeLongitude
                let facing = cos(longitude)
                guard facing > 0.04 else { continue }
                var eye = context
                eye.translateBy(x: radius * sin(longitude) * cos(latitude), y: radius * sin(latitude) - side * sin(expression.yaw))
                eye.rotate(by: .degrees(expression.eyeTilt * 0.6))
                let width = 6.4 * expression.eyeWidth * facing
                let height = 10.8 * expression.eyeHeight * eyelid * (0.7 + 0.3 * cos(latitude))
                eye.fill(Path(ellipseIn: CGRect(x: -width / 2, y: -height / 2, width: width, height: height)), with: .color(ink))
            }
            let facing = cos(expression.yaw)
            if facing > 0.15 {
                let mouthLatitude = Self.mouthLatitude + expression.pitch
                var mouth = context
                mouth.translateBy(x: radius * sin(expression.yaw) * cos(mouthLatitude), y: radius * sin(mouthLatitude))
                let half = expression.mouthWidth / 2 * facing
                if expression.mouthOpen > 0.4 {
                    let width = max(3, half * 0.9), height = expression.mouthOpen * 1.4
                    mouth.fill(Path(ellipseIn: CGRect(x: -width / 2, y: -height / 2, width: width, height: height)), with: .color(ink))
                } else {
                    var lip = Path()
                    lip.move(to: CGPoint(x: -half, y: 0))
                    lip.addQuadCurve(to: CGPoint(x: half, y: -1), control: CGPoint(x: half * 0.1, y: 1 + expression.smile * 1.2))
                    mouth.stroke(lip, with: .color(ink), style: StrokeStyle(lineWidth: 2.5, lineCap: .round))
                }
            }
        }
        .frame(width: size, height: size)
        .scaleEffect(x: 1 + expression.squash * 0.03, y: 1 - expression.squash * 0.05, anchor: .bottom)
        .offset(y: -sin(time * .pi / 1.1) * size * 0.012 * thinking)
        .rotationEffect(.degrees(expression.roll * 0.6))
    }
}
