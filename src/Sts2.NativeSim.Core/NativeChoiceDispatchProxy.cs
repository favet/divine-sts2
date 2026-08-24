using System.Reflection;

namespace Sts2.NativeSim.Core;

/// Runtime adapter for STS2's assembly-owned test selector interface. The proxy
/// never selects a card itself; it delegates to the persistent environment.
public class NativeChoiceDispatchProxy : DispatchProxy
{
    public Func<MethodInfo, object?[]?, object?>? Handler { get; set; }

    protected override object? Invoke(MethodInfo? targetMethod, object?[]? args)
        => Handler?.Invoke(targetMethod ?? throw new MissingMethodException(), args)
           ?? throw new InvalidOperationException("Native choice proxy is not bound.");
}
