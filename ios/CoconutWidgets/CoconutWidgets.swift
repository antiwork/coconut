import SwiftUI
import WidgetKit

/// The widgets of Coconut (ios/HomeScreen/CoconutGlance.swift): what needs the person and what works on the Home Screen
/// and the Lock Screen, New agent as a widget and as a control, and the Live Activity of the work of the agents.
@main
struct CoconutWidgets: WidgetBundle {
    var body: some Widget {
        NowWidget()
        NewAgentWidget()
        NewAgentControl()
        WorkLiveActivity()
    }
}
