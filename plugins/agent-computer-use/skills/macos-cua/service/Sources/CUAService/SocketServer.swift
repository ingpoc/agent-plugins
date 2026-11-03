import Darwin
import Foundation
import Logging
import NIOCore
import NIOPosix

final class SocketServer: @unchecked Sendable {
    private let socketPath: String
    private let logger: Logger
    private let handler: @Sendable (JSONRPCRequest) async -> JSONRPCResponse
    private let allowedUID: uid_t
    private let readTimeout: TimeAmount
    private let connectionLimit: Int
    private let connectionLock = NSLock()
    private var connectionCount = 0
    private var group: MultiThreadedEventLoopGroup?
    private var channel: Channel?

    init(
        socketPath: String,
        logger: Logger,
        allowedUID: uid_t = getuid(),
        readTimeout: TimeAmount = .seconds(10),
        connectionLimit: Int = 64,
        handler: @escaping @Sendable (JSONRPCRequest) async -> JSONRPCResponse
    ) {
        self.socketPath = socketPath
        self.logger = logger
        self.allowedUID = allowedUID
        self.readTimeout = readTimeout
        self.connectionLimit = connectionLimit
        self.handler = handler
    }

    func start() throws {
        do {
            let dir = (socketPath as NSString).deletingLastPathComponent
            try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true,
                                                     attributes: [.posixPermissions: 0o700])
            var st = stat()
            guard lstat(dir, &st) == 0, st.st_mode & S_IFMT == S_IFDIR, st.st_uid == getuid(),
                  chmod(dir, 0o700) == 0 else { throw SocketAccessError.unsafePath }
            if lstat(socketPath, &st) == 0 {
                guard st.st_mode & S_IFMT == S_IFSOCK, st.st_uid == getuid() else {
                    throw SocketAccessError.unsafePath
                }
                guard unlink(socketPath) == 0 else { throw SocketAccessError.unsafePath }
            } else if errno != ENOENT {
                throw SocketAccessError.unsafePath
            }
        } catch {
            logger.error("Socket path preparation failed: \(error)")
            throw error
        }

        let group = MultiThreadedEventLoopGroup(numberOfThreads: 2)
        self.group = group
        // Keep half-close disabled: unread closed peers previously exhausted the service's descriptors.
        let bootstrap = ServerBootstrap(group: group)
            .serverChannelOption(ChannelOptions.backlog, value: 16)
            .childChannelInitializer { [weak self] channel in
                guard let self else { return channel.eventLoop.makeFailedFuture(SocketAccessError.untrustedPeer) }
                guard let socket = channel as? SocketOptionProvider else {
                    return channel.eventLoop.makeFailedFuture(SocketAccessError.untrustedPeer)
                }
                let peer: EventLoopFuture<xucred> = socket.unsafeGetSocketOption(
                    level: .init(rawValue: SOL_LOCAL), name: .init(rawValue: LOCAL_PEERCRED)
                )
                return peer.flatMap { [self] credentials in
                    guard credentials.cr_version == UInt32(XUCRED_VERSION), credentials.cr_uid == self.allowedUID else {
                        return channel.eventLoop.makeFailedFuture(SocketAccessError.untrustedPeer)
                    }
                    self.connectionLock.lock()
                    let admitted = self.connectionCount < self.connectionLimit
                    if admitted { self.connectionCount += 1 }
                    self.connectionLock.unlock()
                    guard admitted else {
                        return channel.eventLoop.makeFailedFuture(SocketAccessError.connectionLimit)
                    }
                    let rpc = RPCChannelHandler(handler: self.handler, logger: self.logger, readTimeout: self.readTimeout) { [weak self] in
                        guard let self else { return }
                        self.connectionLock.lock()
                        self.connectionCount -= 1
                        self.connectionLock.unlock()
                    }
                    return channel.pipeline.addHandler(rpc).flatMapError { error in
                        rpc.releaseConnection()
                        return channel.eventLoop.makeFailedFuture(error)
                    }
                }
            }

        do {
            let ch = try bootstrap.bind(unixDomainSocketPath: socketPath).wait()
            do {
                try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: socketPath)
            } catch {
                try? ch.close().wait()
                throw error
            }
            channel = ch
            logger.info("Listening", metadata: ["path": "\(socketPath)"])
        } catch {
            try? group.syncShutdownGracefully()
            self.group = nil
            throw error
        }
    }

    func stop() {
        try? channel?.close().wait()
        try? group?.syncShutdownGracefully()
    }
}

private enum SocketAccessError: Error {
    case unsafePath, untrustedPeer, connectionLimit
}

private final class RPCChannelHandler: ChannelInboundHandler, @unchecked Sendable {
    typealias InboundIn = ByteBuffer

    private let handler: @Sendable (JSONRPCRequest) async -> JSONRPCResponse
    private let logger: Logger
    private let readTimeout: TimeAmount
    private let onClose: @Sendable () -> Void
    private var released = false
    private var buffer = Data()
    private var deadline: Scheduled<Void>?
    private var inFlight = 0

    init(handler: @escaping @Sendable (JSONRPCRequest) async -> JSONRPCResponse,
         logger: Logger, readTimeout: TimeAmount, onClose: @escaping @Sendable () -> Void) {
        self.handler = handler
        self.logger = logger
        self.readTimeout = readTimeout
        self.onClose = onClose
    }

    func releaseConnection() {
        guard !released else { return }
        released = true
        deadline?.cancel()
        deadline = nil
        onClose()
    }

    func channelActive(context: ChannelHandlerContext) {
        armDeadline(context.channel)
        context.fireChannelActive()
    }

    private func armDeadline(_ channel: Channel) {
        guard deadline == nil else { return }
        deadline = channel.eventLoop.scheduleTask(in: readTimeout) { channel.close(promise: nil) }
    }

    func channelRead(context: ChannelHandlerContext, data: NIOAny) {
        armDeadline(context.channel)
        var incoming = unwrapInboundIn(data)
        if let bytes = incoming.readBytes(length: incoming.readableBytes) { buffer.append(contentsOf: bytes) }
        do {
            while let request = try FrameCodec.decode(from: &buffer) {
                deadline?.cancel()
                deadline = nil
                guard inFlight < 8 else { context.close(promise: nil); return }
                inFlight += 1
                let handler = self.handler, channel = context.channel, allocator = channel.allocator
                Task.detached { [weak self] in
                    let response = await handler(request)
                    let encoded = Result { try FrameCodec.encode(response) }
                    channel.eventLoop.execute { [weak self] in
                        guard let self else { return }
                        self.inFlight -= 1
                        guard channel.isActive else { return }
                        do {
                            let frame = try encoded.get()
                            var output = allocator.buffer(capacity: frame.count)
                            output.writeBytes(frame)
                            channel.writeAndFlush(output, promise: nil)
                        } catch { channel.close(promise: nil) }
                    }
                }
                if !buffer.isEmpty { armDeadline(context.channel) }
            }
        } catch {
            logger.warning("Invalid RPC frame; closing connection")
            buffer.removeAll()
            context.close(promise: nil)
        }
    }

    func channelInactive(context: ChannelHandlerContext) {
        releaseConnection()
        context.fireChannelInactive()
    }

    func handlerRemoved(context: ChannelHandlerContext) { releaseConnection() }

    func errorCaught(context: ChannelHandlerContext, error: Error) {
        logger.warning("Connection error: \(error)")
        context.close(promise: nil)
    }
}
