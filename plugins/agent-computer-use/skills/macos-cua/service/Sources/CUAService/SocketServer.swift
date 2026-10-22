import Foundation
import Logging
import NIOCore
import NIOPosix

final class SocketServer: @unchecked Sendable {
    private let socketPath: String
    private let logger: Logger
    private let handler: @Sendable (JSONRPCRequest) async -> JSONRPCResponse
    private var group: MultiThreadedEventLoopGroup?
    private var channel: Channel?

    init(
        socketPath: String,
        logger: Logger,
        handler: @escaping @Sendable (JSONRPCRequest) async -> JSONRPCResponse
    ) {
        self.socketPath = socketPath
        self.logger = logger
        self.handler = handler
    }

    func start() {
        try? FileManager.default.removeItem(atPath: socketPath)
        let dir = (socketPath as NSString).deletingLastPathComponent
        try? FileManager.default.createDirectory(
            atPath: dir, withIntermediateDirectories: true
        )

        let group = MultiThreadedEventLoopGroup(numberOfThreads: 2)
        self.group = group
        let handler = self.handler
        let logger = self.logger

        // No allowRemoteHalfClosure: with it set, a peer that closes its
        // write side leaves the child channel open forever (nothing consumes
        // ChannelEvent.inputClosed), leaking one fd per client. At the 256 fd
        // soft limit the process still accepts connections and then drops
        // them, so every RPC fails while the service looks alive. Clients
        // here send one framed request and close the whole socket.
        let bootstrap = ServerBootstrap(group: group)
            .serverChannelOption(ChannelOptions.backlog, value: 16)
            .childChannelInitializer { channel in
                channel.pipeline.addHandler(
                    RPCChannelHandler(handler: handler, logger: logger)
                )
            }

        Task.detached {
            do {
                let ch = try await bootstrap.bind(
                    unixDomainSocketPath: self.socketPath
                ).get()
                self.channel = ch
                logger.info("Listening", metadata: ["path": "\(self.socketPath)"])
            } catch {
                logger.error("Bind failed: \(error)")
            }
        }
    }

    func stop() {
        try? channel?.close().wait()
        try? group?.syncShutdownGracefully()
    }
}

private final class RPCChannelHandler: ChannelInboundHandler, @unchecked Sendable {
    typealias InboundIn = ByteBuffer

    private let handler: @Sendable (JSONRPCRequest) async -> JSONRPCResponse
    private let logger: Logger
    private var buffer = Data()

    init(
        handler: @escaping @Sendable (JSONRPCRequest) async -> JSONRPCResponse,
        logger: Logger
    ) {
        self.handler = handler
        self.logger = logger
    }

    func channelRead(context: ChannelHandlerContext, data: NIOAny) {
        var incoming = unwrapInboundIn(data)
        if let bytes = incoming.readBytes(length: incoming.readableBytes) {
            buffer.append(contentsOf: bytes)
        }

        while let request = FrameCodec.decode(from: &buffer) {
            let handler = self.handler
            let channel = context.channel
            let allocator = channel.allocator
            // Run handler off the event loop to avoid blocking NIO
            Task.detached {
                let response = await handler(request)
                guard let frameData = try? FrameCodec.encode(response) else { return }
                channel.eventLoop.execute {
                    var buf = allocator.buffer(capacity: frameData.count)
                    buf.writeBytes(frameData)
                    channel.writeAndFlush(buf, promise: nil)
                }
            }
        }
    }

    func errorCaught(context: ChannelHandlerContext, error: Error) {
        logger.warning("Connection error: \(error)")
        context.close(promise: nil)
    }
}
