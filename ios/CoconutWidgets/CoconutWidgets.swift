import SwiftUI
import WidgetKit

/// The widgets of Coconut (ios/HomeScreen/CoconutGlance.swift): what needs the person and what works on the Home Screen
/// and the Lock Screen, New agent as a widget and as a control, and the Live Activity of the work of the agents.
@main
struct CoconutWidgets: WidgetBundle {
    var body: some Widget {
        NowWidget()
        // The same widget with a push handler, so the Mac can reload it after the person leaves the app.
        if #available(iOS 26.0, *) { NowPushWidget() }
        NewAgentWidget()
        NewAgentControl()
        WorkLiveActivity()
    }
}
