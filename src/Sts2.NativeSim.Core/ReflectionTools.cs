using System.Collections;
using System.Reflection;

namespace Sts2.NativeSim.Core;

internal static class ReflectionTools
{
    private const BindingFlags All = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static;

    public static object? InvokeStatic(Type type, string name, params object?[] args)
        => FindMethod(type, name, isStatic: true, args).Invoke(null, args);

    public static object? Invoke(object target, string name, params object?[] args)
        => FindMethod(target.GetType(), name, isStatic: false, args).Invoke(target, args);

    public static object? GetStatic(Type type, string name)
        => type.GetProperty(name, All)?.GetValue(null) ?? type.GetField(name, All)?.GetValue(null);

    public static void SetStatic(Type type, string name, object? value)
    {
        PropertyInfo? property = type.GetProperty(name, All);
        if (property?.SetMethod is not null)
        {
            property.SetValue(null, value);
            return;
        }
        FieldInfo field = type.GetField(name, All) ?? throw new MissingFieldException(type.FullName, name);
        field.SetValue(null, value);
    }

    public static object? Get(object target, string name)
        => target.GetType().GetProperty(name, All)?.GetValue(target) ?? target.GetType().GetField(name, All)?.GetValue(target);

    public static void Set(object target, string name, object? value)
    {
        PropertyInfo? property = target.GetType().GetProperty(name, All);
        if (property?.SetMethod is not null)
        {
            property.SetValue(target, value);
            return;
        }
        FieldInfo field = target.GetType().GetField(name, All) ?? throw new MissingFieldException(target.GetType().FullName, name);
        field.SetValue(target, value);
    }

    public static object Create(Type type, params object?[] args)
    {
        ConstructorInfo constructor = type.GetConstructors(All)
            .Where(candidate => !candidate.IsStatic)
            .Where(candidate => candidate.GetParameters().Length == args.Length)
            .FirstOrDefault(candidate => ParametersAccept(candidate.GetParameters(), args))
            ?? throw new MissingMethodException(type.FullName, $".ctor({string.Join(',', args.Select(a => a?.GetType().Name ?? "null"))})");
        return constructor.Invoke(args);
    }

    public static Array EmptyArray(Type elementType) => Array.CreateInstance(elementType, 0);

    public static IReadOnlyList<object?> Enumerate(object? value)
    {
        if (value is not IEnumerable sequence)
            throw new InvalidOperationException($"Expected enumerable, got {value?.GetType().FullName ?? "null"}.");
        return sequence.Cast<object?>().ToArray();
    }

    public static Exception Unwrap(Exception exception)
    {
        while (exception is TargetInvocationException { InnerException: not null } invocation)
            exception = invocation.InnerException!;
        return exception;
    }

    private static MethodInfo FindMethod(Type type, string name, bool isStatic, object?[] args)
        => type.GetMethods(All)
            .Where(method => method.Name == name && method.IsStatic == isStatic && !method.IsGenericMethodDefinition)
            .Where(method => method.GetParameters().Length == args.Length)
            .FirstOrDefault(method => ParametersAccept(method.GetParameters(), args))
            ?? throw new MissingMethodException(type.FullName, name);

    private static bool ParametersAccept(ParameterInfo[] parameters, object?[] args)
    {
        for (int index = 0; index < parameters.Length; index++)
        {
            if (args[index] is null)
            {
                if (parameters[index].ParameterType.IsValueType && Nullable.GetUnderlyingType(parameters[index].ParameterType) is null)
                    return false;
                continue;
            }
            if (!parameters[index].ParameterType.IsInstanceOfType(args[index]))
                return false;
        }
        return true;
    }
}
