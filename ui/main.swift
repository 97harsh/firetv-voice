// FireTVVoiceUI: menu bar icon + screen-edge glow + small pop-up.
// Listens for UDP JSON events from the engine on 127.0.0.1:47811. Needs no permissions.
//   owner    -> soft glow: "that's you"
//   command  -> bright glow + pop-up "open netflix" (green = done, violet = observe mode, red = failed)
//   heard    -> dim glow + pop-up "not for the TV"
//   status   -> heartbeat (TV connection, mode, listening, enrolled) drives the menu bar icon
import AppKit
import Network
import QuartzCore

let ftv = "/opt/homebrew/bin/ftv"

final class GlowWindow: NSWindow {
    let gradient = CAGradientLayer()
    let mask = CAShapeLayer()
    let host = CALayer()

    init(screen: NSScreen) {
        super.init(contentRect: screen.frame, styleMask: .borderless, backing: .buffered, defer: false)
        isOpaque = false
        backgroundColor = .clear
        ignoresMouseEvents = true
        level = .screenSaver
        hasShadow = false
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        let v = NSView(frame: NSRect(origin: .zero, size: screen.frame.size))
        v.wantsLayer = true
        v.layerUsesCoreImageFilters = true
        contentView = v
        host.frame = v.bounds
        host.opacity = 0
        v.layer!.addSublayer(host)
        let d = hypot(v.bounds.width, v.bounds.height)
        gradient.type = .conic
        gradient.startPoint = CGPoint(x: 0.5, y: 0.5)
        gradient.endPoint = CGPoint(x: 0.5, y: 0)
        gradient.frame = CGRect(x: (v.bounds.width - d) / 2, y: (v.bounds.height - d) / 2, width: d, height: d)
        let container = CALayer()
        container.frame = v.bounds
        container.addSublayer(gradient)
        mask.frame = v.bounds
        mask.path = CGPath(roundedRect: v.bounds.insetBy(dx: 6, dy: 6), cornerWidth: 14, cornerHeight: 14, transform: nil)
        mask.fillColor = nil
        mask.strokeColor = NSColor.white.cgColor
        mask.lineWidth = 22
        container.mask = mask
        host.addSublayer(container)
        if let blur = CIFilter(name: "CIGaussianBlur", parameters: [kCIInputRadiusKey: 10]) { host.filters = [blur] }
        let spin = CABasicAnimation(keyPath: "transform.rotation.z")
        spin.fromValue = 0; spin.toValue = -2 * Double.pi; spin.duration = 4; spin.repeatCount = .infinity
        gradient.add(spin, forKey: "spin")
    }

    func flash(_ colors: [NSColor], peak: Float, hold: Double) {
        gradient.colors = (colors + [colors[0]]).map { $0.cgColor }
        host.removeAnimation(forKey: "fade")
        let a = CAKeyframeAnimation(keyPath: "opacity")
        a.values = [host.presentation()?.opacity ?? 0, peak, peak, 0]
        a.keyTimes = [0, 0.12, NSNumber(value: 0.12 + 0.7 * hold / (hold + 0.6)), 1]
        a.duration = hold + 0.6
        host.opacity = 0
        host.add(a, forKey: "fade")
        orderFrontRegardless()
    }
}

final class Toast: NSPanel {
    let label = NSTextField(labelWithString: "")
    let sub = NSTextField(labelWithString: "")
    let dot = NSView()
    var hideWork: DispatchWorkItem?

    init() {
        super.init(contentRect: NSRect(x: 0, y: 0, width: 460, height: 64), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        isOpaque = false; backgroundColor = .clear; level = .screenSaver; hasShadow = true
        ignoresMouseEvents = true
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        let fx = NSVisualEffectView(frame: contentRect(forFrameRect: frame))
        fx.material = .hudWindow; fx.state = .active; fx.blendingMode = .behindWindow
        fx.wantsLayer = true; fx.layer!.cornerRadius = 18; fx.layer!.masksToBounds = true
        contentView = fx
        dot.frame = NSRect(x: 18, y: 26, width: 12, height: 12)
        dot.wantsLayer = true; dot.layer!.cornerRadius = 6
        fx.addSubview(dot)
        label.frame = NSRect(x: 42, y: 30, width: 400, height: 22)
        label.font = .systemFont(ofSize: 16, weight: .semibold); label.textColor = .labelColor
        label.lineBreakMode = .byTruncatingTail
        sub.frame = NSRect(x: 42, y: 11, width: 400, height: 17)
        sub.font = .systemFont(ofSize: 12); sub.textColor = .secondaryLabelColor
        sub.lineBreakMode = .byTruncatingTail
        fx.addSubview(label); fx.addSubview(sub)
    }

    func show(_ title: String, _ subtitle: String, _ color: NSColor, secs: Double = 3.0) {
        label.stringValue = title; sub.stringValue = subtitle
        dot.layer!.backgroundColor = color.cgColor
        if let s = NSScreen.main {
            let f = s.visibleFrame
            setFrameOrigin(NSPoint(x: f.midX - frame.width / 2, y: f.maxY - frame.height - 14))
        }
        alphaValue = 1
        orderFrontRegardless()
        hideWork?.cancel()
        let w = DispatchWorkItem { [weak self] in
            NSAnimationContext.runAnimationGroup { $0.duration = 0.4; self?.animator().alphaValue = 0 }
        }
        hideWork = w
        DispatchQueue.main.asyncAfter(deadline: .now() + secs, execute: w)
    }
}

final class App: NSObject, NSApplicationDelegate {
    var item: NSStatusItem!
    var glows: [GlowWindow] = []
    let toast = Toast()
    var status: [String: Any] = [:]
    var lastBeat = Date.distantPast
    var lastHeard = "—"
    var listener: NWListener?
    let menu = NSMenu()

    func applicationDidFinishLaunching(_ n: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        item.menu = menu
        menu.delegate = self
        rebuildGlows()
        NotificationCenter.default.addObserver(forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main) { [weak self] _ in self?.rebuildGlows() }
        listen()
        Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.refreshIcon() }
        refreshIcon()
    }

    func rebuildGlows() {
        glows.forEach { $0.orderOut(nil) }
        glows = NSScreen.screens.map { GlowWindow(screen: $0) }
    }

    func listen() {
        let params = NWParameters.udp
        params.requiredLocalEndpoint = .hostPort(host: "127.0.0.1", port: 47811)
        params.allowLocalEndpointReuse = true
        guard let l = try? NWListener(using: params) else { return }
        l.newConnectionHandler = { [weak self] c in
            c.start(queue: .main)
            self?.receive(c)
        }
        l.start(queue: .main)
        listener = l
    }

    func receive(_ c: NWConnection) {
        c.receiveMessage { [weak self] data, _, _, err in
            if let d = data, let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] { self?.handle(o) }
            if err == nil { self?.receive(c) }
        }
    }

    let blue = NSColor(calibratedRed: 0.35, green: 0.65, blue: 1, alpha: 1)
    let cyan = NSColor(calibratedRed: 0.3, green: 0.95, blue: 0.95, alpha: 1)
    let violet = NSColor(calibratedRed: 0.7, green: 0.45, blue: 1, alpha: 1)
    let green = NSColor(calibratedRed: 0.3, green: 0.9, blue: 0.5, alpha: 1)
    let pink = NSColor(calibratedRed: 1, green: 0.45, blue: 0.75, alpha: 1)

    func glow(_ colors: [NSColor], _ peak: Float, _ hold: Double) { glows.forEach { $0.flash(colors, peak: peak, hold: hold) } }

    func handle(_ o: [String: Any]) {
        let t = o["t"] as? String ?? ""
        switch t {
        case "status":
            status = o; lastBeat = Date(); refreshIcon()
        case "owner":
            glow([blue, cyan, violet], 0.45, 0.8)
        case "command":
            let intent = o["intent"] as? String ?? ""
            let text = o["text"] as? String ?? ""
            let mode = o["mode"] as? String ?? "live"
            lastHeard = "\(text) → \(intent)"
            if let e = o["error"] as? String, !e.isEmpty {
                glow([.systemRed, .systemOrange], 0.9, 1.2)
                toast.show(intent, "Couldn't reach the TV: \(e)", .systemRed, secs: 5)
            } else if mode == "observe" || mode == "enroll" {
                glow([violet, pink, blue], 0.9, 1.0)
                toast.show("Would: \(intent)", "“\(text)”  ·  \(mode == "enroll" ? "enrolling" : "observe mode, TV untouched")", violet)
            } else {
                glow([green, cyan, blue], 1.0, 1.0)
                let ms = (o["ms"] as? Double).map { String(format: "  ·  %.0f ms", $0) } ?? ""
                toast.show(intent, "“\(text)”\(ms)", green)
            }
        case "heard":
            let text = o["text"] as? String ?? ""
            let why = o["why"] as? String ?? "not for the TV"
            if text.isEmpty { return }
            lastHeard = "\(text) (\(why))"
            glow([blue, violet], 0.25, 0.6)
            toast.show("“\(text)”", why, .systemGray, secs: 2.2)
        case "enroll":
            let phase = o["phase"] as? String ?? ""
            if phase == "listening" { toast.show("Learning your voice", "Talk naturally…", blue, secs: 4); glow([blue, cyan], 0.5, 1.5) }
            if phase == "done" { toast.show("Voice learned", "\(o["clips"] ?? "") phrases", green, secs: 4); glow([green, cyan], 0.9, 1.5) }
            if phase == "failed" { toast.show("Enrollment failed", "see terminal", .systemRed, secs: 4) }
        default: break
        }
    }

    var alive: Bool { Date().timeIntervalSince(lastBeat) < 6 }

    func refreshIcon() {
        let mode = status["mode"] as? String ?? "?"
        let tv = status["tv"] as? String ?? "?"
        let listening = status["listening"] as? Bool ?? true
        var name = "waveform"; var tint: NSColor = .labelColor
        if !alive { name = "waveform.slash"; tint = .systemGray }
        else if tv != "ok" { name = "exclamationmark.triangle"; tint = .systemOrange }
        else if !listening { name = "pause.circle"; tint = .systemGray }
        else if mode == "observe" { name = "eye"; tint = violet }
        else { name = "waveform"; tint = green }
        let img = NSImage(systemSymbolName: name, accessibilityDescription: "FireTV Voice")
        let cfg = NSImage.SymbolConfiguration(paletteColors: [tint])
        item.button?.image = img?.withSymbolConfiguration(cfg)
        item.button?.toolTip = alive ? "FireTV Voice — \(mode), TV \(tv)" : "FireTV Voice — service not running"
    }

    @objc func run(_ sender: NSMenuItem) {
        guard let args = sender.representedObject as? [String] else { return }
        let p = Process(); p.executableURL = URL(fileURLWithPath: ftv); p.arguments = args
        try? p.run()
    }
    @objc func openLog() { NSWorkspace.shared.open(URL(fileURLWithPath: NSHomeDirectory() + "/Library/Logs/FireTVVoice/service.log")) }
    @objc func testGlow() {
        handle(["t": "owner"])
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { self.handle(["t": "command", "text": "Open Netflix.", "intent": "open netflix", "mode": self.status["mode"] as? String ?? "observe", "ms": 480.0]) }
    }
}

extension App: NSMenuDelegate {
    func menuNeedsUpdate(_ m: NSMenu) {
        m.removeAllItems()
        func info(_ s: String) { let i = NSMenuItem(title: s, action: nil, keyEquivalent: ""); i.isEnabled = false; m.addItem(i) }
        func act(_ s: String, _ args: [String]) {
            let i = NSMenuItem(title: s, action: #selector(App.run(_:)), keyEquivalent: ""); i.target = self; i.representedObject = args; m.addItem(i)
        }
        if !alive { info("Service not running"); act("Start service", ["start"]) } else {
            let mode = status["mode"] as? String ?? "?"
            let tv = status["tv"] as? String ?? "?"
            info("Mode: \(mode == "live" ? "LIVE — controls the TV" : "OBSERVE — TV untouched")")
            info("TV: \(tv == "ok" ? "connected" : tv)")
            info("Mic: \((status["audio"] as? Bool ?? false) ? "listening" : "no audio!")")
            info("Voice: \((status["enrolled"] as? Bool ?? false) ? "enrolled (only you)" : "not enrolled — say \"TV, …\"")")
            info("Last: \(lastHeard)")
            m.addItem(.separator())
            if mode == "live" { act("Switch to Observe (don't touch TV)", ["observe"]) } else { act("Switch to LIVE (control the TV)", ["live"]) }
            if status["listening"] as? Bool ?? true { act("Pause listening", ["pause"]) } else { act("Resume listening", ["resume"]) }
            act("Last one was wrong", ["wrong"])
            act("Reconnect TV", ["reload"])
            act("Restart service", ["restart"])
        }
        m.addItem(.separator())
        let t = NSMenuItem(title: "Test glow", action: #selector(App.testGlow), keyEquivalent: ""); t.target = self; m.addItem(t)
        let l = NSMenuItem(title: "Open log", action: #selector(App.openLog), keyEquivalent: ""); l.target = self; m.addItem(l)
        m.addItem(NSMenuItem(title: "Quit UI", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
    }
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
