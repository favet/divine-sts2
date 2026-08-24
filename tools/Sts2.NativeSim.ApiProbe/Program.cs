using System.Reflection;
using System.Runtime.Loader;

if (args.Length == 0)
{
    Console.Error.WriteLine("Usage: ApiProbe <full-or-partial-type-name> [game-data-directory]");
    return 2;
}

string gameAssemblyDirectory = args.Length > 1
    ? Path.GetFullPath(args[1])
    : Environment.GetEnvironmentVariable("STS2_GAME_DATA")
        ?? throw new ArgumentException("Pass the game data directory or set STS2_GAME_DATA.");
AssemblyLoadContext.Default.Resolving += (_, name) =>
{
    string candidate = Path.Combine(gameAssemblyDirectory, $"{name.Name}.dll");
    return File.Exists(candidate) ? AssemblyLoadContext.Default.LoadFromAssemblyPath(candidate) : null;
};
Assembly assembly = AssemblyLoadContext.Default.LoadFromAssemblyPath(Path.Combine(gameAssemblyDirectory, "sts2.dll"));
foreach (Type type in assembly.GetTypes().Where(type => type.FullName?.Contains(args[0], StringComparison.OrdinalIgnoreCase) == true).OrderBy(type => type.FullName))
{
    Console.WriteLine(type.FullName);
    foreach (MemberInfo member in type.GetMembers(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static | BindingFlags.Instance | BindingFlags.DeclaredOnly).OrderBy(member => member.Name))
        Console.WriteLine($"  {member.MemberType}: {member}");
}
return 0;
