import SwiftUI

@main
struct SidePulseApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            ContentView()
                .task {
#if DEBUG && SIDEPULSE_MAIN_APP
                    if DemoData.isEnabled {
                        AppModel.shared.liveMonitorServerURL = DemoData.serverURL
                        AppModel.shared.refreshFolderStatus()
                        if #available(iOS 17.2, *), DemoData.wantsLiveActivity {
                            DemoData.startLiveActivity()
                        }
                        return
                    }
#endif
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
