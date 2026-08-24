using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using MegaCrit.Sts2.Core.AutoSlay;
using MegaCrit.Sts2.Core.Nodes;

namespace Sts2.NativeSim.FullAppBridge;

public static class FullAppBridgeServer
{
    private static TcpListener? _listener;
    private static TcpClient? _client;
    private static NetworkStream? _stream;
    private static StreamWriter? _writer;
    private static StreamReader? _reader;
    private static TaskCompletionSource<string>? _pendingActionTcs;
    private static TaskCompletionSource<bool>? _initialBoundaryTcs;
    private static readonly object SyncLock = new();

    public static int BoundPort { get; private set; }
    public static ObservationDto? CurrentObservation { get; set; }
    public static List<LegalActionDto> CurrentLegalActions { get; set; } = new();
    public static List<string> ActionHistory { get; } = new();
    public static List<string> StateHashHistory { get; } = new();

    public static string RequestedSeed { get; private set; } = "A1B2C3D4E5";
    public static string RequestedCharacter { get; private set; } = "IRONCLAD";
    public static int RequestedAscension { get; private set; } = 0;
    public static bool IsRunStarted { get; private set; }

    public static void Start(int preferredPort, string portFilePath)
    {
        string forceChar = Environment.GetEnvironmentVariable("STS2_FORCE_CHARACTER") ?? "";
        if (!string.IsNullOrWhiteSpace(forceChar))
        {
            RequestedCharacter = forceChar;
        }

        _listener = new TcpListener(IPAddress.Loopback, preferredPort);
        _listener.Start();
        BoundPort = ((IPEndPoint)_listener.LocalEndpoint).Port;

        if (!string.IsNullOrWhiteSpace(portFilePath))
        {
            Directory.CreateDirectory(Path.GetDirectoryName(portFilePath)!);
            File.WriteAllText(portFilePath, BoundPort.ToString());
        }

        Task.Run(ListenLoopAsync);
    }

    private static async Task ListenLoopAsync()
    {
        while (_listener is not null)
        {
            try
            {
                TcpClient client = await _listener.AcceptTcpClientAsync();
                lock (SyncLock)
                {
                    _client?.Dispose();
                    _client = client;
                    _stream = client.GetStream();
                    _writer = new StreamWriter(_stream, new UTF8Encoding(false)) { AutoFlush = true };
                    _reader = new StreamReader(_stream, new UTF8Encoding(false));
                }

                _ = HandleClientAsync(_reader, _writer);
            }
            catch
            {
                // listener closed or error
                break;
            }
        }
    }

    private static async Task HandleClientAsync(StreamReader reader, StreamWriter writer)
    {
        while (true)
        {
            string? line = await reader.ReadLineAsync();
            if (line is null) break;
            if (string.IsNullOrWhiteSpace(line)) continue;

            try
            {
                RpcRequest? request = JsonSerializer.Deserialize<RpcRequest>(line);
                if (request is null) continue;

                object? result = await DispatchRequestAsync(request.Method, request.Params);
                var response = new RpcResponse
                {
                    Id = request.Id,
                    Result = result,
                };
                string responseJson = JsonSerializer.Serialize(response);
                await writer.WriteLineAsync(responseJson);
            }
            catch (Exception ex)
            {
                var errorResponse = new RpcResponse
                {
                    Id = 0,
                    Error = ex.Message,
                };
                await writer.WriteLineAsync(JsonSerializer.Serialize(errorResponse));
            }
        }
    }

    private static async Task<object?> DispatchRequestAsync(string method, Dictionary<string, object?>? parameters)
    {
        switch (method.ToLowerInvariant())
        {
            case "hello":
                return new Dictionary<string, object?>
                {
                    ["status"] = "ready",
                    ["version"] = "0.1.0+59260271157f76a2896f0eab5bc6ea1245d8b314",
                    ["pid"] = Environment.ProcessId,
                    ["bound_port"] = BoundPort,
                };

            case "start_run":
                if (parameters is not null)
                {
                    if (parameters.TryGetValue("seed", out var s) && s is not null)
                        RequestedSeed = s.ToString()!;
                    if (parameters.TryGetValue("character", out var c) && c is not null)
                        RequestedCharacter = c.ToString()!;
                    if (parameters.TryGetValue("ascension", out var a) && a is not null && int.TryParse(a.ToString(), out int asc))
                        RequestedAscension = asc;
                }

                _initialBoundaryTcs = new TaskCompletionSource<bool>();
                IsRunStarted = true;

                // Wait until the game reaches the first decision boundary
                await _initialBoundaryTcs.Task;

                return new Dictionary<string, object?>
                {
                    ["started"] = true,
                    ["seed"] = RequestedSeed,
                    ["character"] = RequestedCharacter,
                    ["ascension"] = RequestedAscension,
                    ["observation"] = CurrentObservation,
                    ["legal_actions"] = CurrentLegalActions,
                };

            case "observe":
                return CurrentObservation;

            case "legal_actions":
                return CurrentLegalActions;

            case "step":
                string actionId = parameters?["action_id"]?.ToString() ?? "";
                if (string.IsNullOrWhiteSpace(actionId))
                    throw new ArgumentException("step requires action_id");

                ActionHistory.Add(actionId);

                _initialBoundaryTcs = new TaskCompletionSource<bool>();
                if (_pendingActionTcs != null && !_pendingActionTcs.Task.IsCompleted)
                {
                    _pendingActionTcs.SetResult(actionId);
                }

                await _initialBoundaryTcs.Task;

                if (CurrentObservation is not null)
                {
                    StateHashHistory.Add(CurrentObservation.StateHash);
                }

                return new Dictionary<string, object?>
                {
                    ["observation"] = CurrentObservation,
                    ["legal_actions"] = CurrentLegalActions,
                };

            case "history":
                return new Dictionary<string, object?>
                {
                    ["seed"] = RequestedSeed,
                    ["character"] = RequestedCharacter,
                    ["ascension"] = RequestedAscension,
                    ["actions"] = ActionHistory,
                    ["state_hashes"] = StateHashHistory,
                };

            case "close":
                _ = Task.Run(async () =>
                {
                    await Task.Delay(50);
                    Environment.Exit(0);
                });
                return new Dictionary<string, object?> { ["closed"] = true };

            default:
                throw new NotSupportedException($"Unknown RPC method: {method}");
        }
    }

    public static async Task<string> WaitForCoordinatorActionAsync(
        string phase,
        bool isTerminal,
        bool isVictory,
        object? contextObject = null)
    {
        AutoSlayer.CurrentWatchdog?.Reset($"Bridge:{phase}");
        var (obs, actions) = FullAppStateTracker.CreateStateSnapshot(phase, isTerminal, isVictory, contextObject);
        CurrentObservation = obs;
        CurrentLegalActions = actions;

        if (StateHashHistory.Count == 0 && obs is not null)
        {
            StateHashHistory.Add(obs.StateHash);
        }

        // Notify that a decision boundary has been reached
        _initialBoundaryTcs?.TrySetResult(true);

        if (isTerminal)
        {
            // Run has finished; keep server alive for final observation/history inspections
            return "terminal_halt";
        }

        _pendingActionTcs = new TaskCompletionSource<string>();
        string chosenAction = await _pendingActionTcs.Task;
        AutoSlayer.CurrentWatchdog?.Reset($"Bridge:{phase}:{chosenAction}");
        return chosenAction;
    }
}
