// FireTVVoice: tiny mic-capture host.
// Owns the microphone permission (TCC attributes it to this app bundle), captures 16 kHz mono
// float32 audio, and streams it to the Python engine on the child's stdin. All logic lives in
// Python, so this binary (and therefore the mic grant) never needs to be rebuilt.
//
// Usage: FireTVVoice <python> <args...>      (set FTV_INPUT=any to use the system default input)
import AVFoundation
import CoreAudio
import Foundation

setvbuf(stdout, nil, _IOLBF, 0)
signal(SIGPIPE, SIG_IGN)
func say(_ s: String) { FileHandle.standardError.write(("[FireTVVoice] " + s + "\n").data(using: .utf8)!) }

let argv = Array(CommandLine.arguments.dropFirst())
guard argv.count >= 1 else { say("usage: FireTVVoice <python> <args...>"); exit(2) }

let activity = ProcessInfo.processInfo.beginActivity(options: [.userInitiated, .latencyCritical],
                                                     reason: "Always-on voice control")
_ = activity

// ---------- microphone permission ----------
func waitForMicPermission() -> Bool {
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized: return true
    case .notDetermined:
        say("requesting microphone permission (macOS will show a prompt)")
        let sem = DispatchSemaphore(value: 0)
        var ok = false
        AVCaptureDevice.requestAccess(for: .audio) { g in ok = g; sem.signal() }
        sem.wait()
        return ok
    default: return false
    }
}
if !waitForMicPermission() {
    say("microphone permission DENIED. Enable FireTVVoice in System Settings > Privacy & Security > Microphone.")
    let n = Process()
    n.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
    n.arguments = ["-e", "display notification \"Allow FireTVVoice in System Settings > Privacy & Security > Microphone, then run: ftv restart\" with title \"FireTV Voice: microphone blocked\""]
    try? n.run(); n.waitUntilExit()
    exit(0)  // SuccessfulExit -> launchd won't respawn-loop
}

// ---------- python engine child ----------
let child = Process()
child.executableURL = URL(fileURLWithPath: argv[0])
child.arguments = Array(argv.dropFirst())
let pipe = Pipe()
child.standardInput = pipe
child.standardOutput = FileHandle.standardOutput
child.standardError = FileHandle.standardError
child.terminationHandler = { p in
    say("engine exited with status \(p.terminationStatus); exiting so launchd restarts us")
    exit(p.terminationStatus == 0 ? 1 : p.terminationStatus)
}
do { try child.run() } catch { say("cannot start engine: \(error)"); exit(1) }
for sig in [SIGTERM, SIGINT] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler { child.terminate(); exit(0) }
    src.resume()
    _ = Unmanaged.passRetained(src)
}
let out = pipe.fileHandleForWriting
let writeQ = DispatchQueue(label: "pcm-writer")

// ---------- audio capture ----------
func builtInMicID() -> AudioDeviceID? {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size)
    var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &ids)
    for id in ids {
        var t = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyTransportType, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        var transport: UInt32 = 0; var ts = UInt32(MemoryLayout<UInt32>.size)
        AudioObjectGetPropertyData(id, &t, 0, nil, &ts, &transport)
        var s = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreamConfiguration, mScope: kAudioDevicePropertyScopeInput, mElement: kAudioObjectPropertyElementMain)
        var ss: UInt32 = 0
        AudioObjectGetPropertyDataSize(id, &s, 0, nil, &ss)
        let buf = UnsafeMutableRawPointer.allocate(byteCount: Int(ss), alignment: 16); defer { buf.deallocate() }
        AudioObjectGetPropertyData(id, &s, 0, nil, &ss, buf)
        let abl = UnsafeMutableAudioBufferListPointer(buf.assumingMemoryBound(to: AudioBufferList.self))
        let inCh = abl.reduce(0) { $0 + Int($1.mNumberChannels) }
        if transport == kAudioDeviceTransportTypeBuiltIn && inCh > 0 { return id }
    }
    return nil
}

let engine = AVAudioEngine()
let target = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16000, channels: 1, interleaved: false)!
let ctlQ = DispatchQueue(label: "engine-control")

func startEngine() {
    let input = engine.inputNode
    if ProcessInfo.processInfo.environment["FTV_INPUT"] != "any", var dev = builtInMicID(), let au = input.audioUnit {
        let st = AudioUnitSetProperty(au, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0, &dev, UInt32(MemoryLayout<AudioDeviceID>.size))
        if st != noErr { say("could not pin built-in mic (\(st)); using default input") }
    }
    let fmt = input.outputFormat(forBus: 0)
    guard fmt.sampleRate > 0, let conv = AVAudioConverter(from: fmt, to: target) else {
        say("no usable input device (format \(fmt)); retrying in 3s")
        ctlQ.asyncAfter(deadline: .now() + 3) { startEngine() }
        return
    }
    input.removeTap(onBus: 0)
    input.installTap(onBus: 0, bufferSize: 1024, format: fmt) { buf, _ in
        let cap = AVAudioFrameCount(Double(buf.frameLength) * 16000 / fmt.sampleRate) + 32
        guard let o = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: cap) else { return }
        var fed = false
        var err: NSError?
        conv.convert(to: o, error: &err) { _, status in
            if fed { status.pointee = .noDataNow; return nil }
            fed = true; status.pointee = .haveData; return buf
        }
        if o.frameLength > 0, let ch = o.floatChannelData {
            let d = Data(bytes: ch[0], count: Int(o.frameLength) * 4)
            writeQ.async { do { try out.write(contentsOf: d) } catch { exit(1) } }
        }
    }
    do { engine.prepare(); try engine.start(); say("capturing: \(fmt.sampleRate) Hz -> 16000 Hz mono") }
    catch { say("engine start failed: \(error); retrying in 3s"); ctlQ.asyncAfter(deadline: .now() + 3) { startEngine() } }
}

NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil) { _ in
    say("audio configuration changed; restarting capture")
    ctlQ.asyncAfter(deadline: .now() + 0.5) { engine.stop(); startEngine() }
}
ctlQ.async { startEngine() }
dispatchMain()
