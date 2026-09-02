import SwiftUI

@main
struct SidePulseApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            ContentView()
                .task {
                    LiveMonitorManager.shared.startIfEnabled(model: AppModel.shared)
                }
        }
        .onChange(of: scenePhase) { phase in
            // The Dot mirror streams while the app is in front; in the
            // background the Mac's silent pushes drive it (DotStatusMirror).
            switch phase {
            case .active:
                DotStatusMirror.shared.start(model: AppModel.shared)
            case .background:
                DotStatusMirror.shared.suspend()
            default:
                break
            }
        }
    }
}
