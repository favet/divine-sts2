using System.Reflection;
using System.Runtime.Loader;

namespace Sts2.NativeSim.Core;

internal sealed class NativeAssemblyContext : IDisposable
{
    private readonly string _assemblyDirectory;
    private readonly AssemblyLoadContext _loadContext;

    public NativeAssemblyContext(string assemblyPath)
    {
        AssemblyPath = Path.GetFullPath(assemblyPath);
        _assemblyDirectory = Path.GetDirectoryName(AssemblyPath)
            ?? throw new ArgumentException("Assembly path has no directory.", nameof(assemblyPath));
        _loadContext = AssemblyLoadContext.GetLoadContext(typeof(NativeAssemblyContext).Assembly)
            ?? AssemblyLoadContext.Default;
        _loadContext.Resolving += Resolve;
        Assembly = _loadContext.LoadFromAssemblyPath(AssemblyPath);
    }

    public string AssemblyPath { get; }
    public Assembly Assembly { get; }

    public Type RequireType(string fullName) => Assembly.GetType(fullName, throwOnError: true)!;

    public Assembly LoadDependency(string fileName)
    {
        string path = Path.Combine(_assemblyDirectory, fileName);
        return _loadContext.LoadFromAssemblyPath(path);
    }

    private Assembly? Resolve(AssemblyLoadContext context, AssemblyName name)
    {
        string candidate = Path.Combine(_assemblyDirectory, name.Name + ".dll");
        return File.Exists(candidate) ? context.LoadFromAssemblyPath(candidate) : null;
    }

    public void Dispose() => _loadContext.Resolving -= Resolve;
}
