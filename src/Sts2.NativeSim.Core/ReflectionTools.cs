using System.Collections;
using System.Collections.Concurrent;
using System.IO;
using System.Linq.Expressions;
using System.Reflection;
using System.Security.Cryptography;

namespace Sts2.NativeSim.Core;

internal static class ReflectionTools
{
    private const BindingFlags All = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static;

    private static readonly ConcurrentDictionary<(Type, string), Func<object, object?>> GetterCache = new();
    private static readonly ConcurrentDictionary<(Type, string), Action<object, object?>> SetterCache = new();
    private static readonly ConcurrentDictionary<(Type, string), Func<object?>> StaticGetterCache = new();
    private static readonly ConcurrentDictionary<(Type, string), Action<object?>> StaticSetterCache = new();
    private static readonly ConcurrentDictionary<string, MethodInfo> MethodCache = new(StringComparer.Ordinal);

    public static string HashFile(string path)
    {
        using FileStream stream = new(path, FileMode.Open, FileAccess.Read, FileShare.Read, 1024 * 1024);
        return Convert.ToHexString(SHA256.HashData(stream));
    }

    public static object? InvokeStatic(Type type, string name, params object?[] args)
        => FindMethod(type, name, isStatic: true, args).Invoke(null, args);

    public static object? Invoke(object target, string name, params object?[] args)
        => FindMethod(target.GetType(), name, isStatic: false, args).Invoke(target, args);

    public static object? GetStatic(Type type, string name)
    {
        Func<object?> getter = StaticGetterCache.GetOrAdd((type, name), key => CreateStaticGetter(key.Item1, key.Item2));
        return getter();
    }

    public static void SetStatic(Type type, string name, object? value)
    {
        Action<object?> setter = StaticSetterCache.GetOrAdd((type, name), key => CreateStaticSetter(key.Item1, key.Item2));
        setter(value);
    }

    public static object? Get(object target, string name)
    {
        if (target is null) return null;
        Type type = target.GetType();
        Func<object, object?> getter = GetterCache.GetOrAdd((type, name), key => CreateGetter(key.Item1, key.Item2));
        return getter(target);
    }

    public static void Set(object target, string name, object? value)
    {
        ArgumentNullException.ThrowIfNull(target);
        Type type = target.GetType();
        Action<object, object?> setter = SetterCache.GetOrAdd((type, name), key => CreateSetter(key.Item1, key.Item2));
        setter(target, value);
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
        if (value is null) return [];
        if (value is IList list)
        {
            object?[] arr = new object?[list.Count];
            for (int i = 0; i < list.Count; i++)
                arr[i] = list[i];
            return arr;
        }
        if (value is IEnumerable sequence)
        {
            List<object?> listResult = [];
            foreach (object? item in sequence)
                listResult.Add(item);
            return listResult;
        }
        throw new InvalidOperationException($"Expected enumerable, got {value.GetType().FullName}.");
    }

    public static Exception Unwrap(Exception exception)
    {
        while (exception is TargetInvocationException { InnerException: not null } invocation)
            exception = invocation.InnerException!;
        return exception;
    }

    private static Func<object, object?> CreateGetter(Type type, string name)
    {
        PropertyInfo? prop = type.GetProperty(name, All);
        if (prop is not null && prop.GetMethod is not null)
        {
            ParameterExpression param = Expression.Parameter(typeof(object), "target");
            Expression castTarget = type.IsValueType ? Expression.Unbox(param, type) : Expression.Convert(param, type);
            Expression call = Expression.Call(castTarget, prop.GetMethod);
            Expression castResult = Expression.Convert(call, typeof(object));
            return Expression.Lambda<Func<object, object?>>(castResult, param).Compile();
        }
        FieldInfo? field = type.GetField(name, All);
        if (field is not null)
        {
            ParameterExpression param = Expression.Parameter(typeof(object), "target");
            Expression castTarget = type.IsValueType ? Expression.Unbox(param, type) : Expression.Convert(param, type);
            Expression access = Expression.Field(castTarget, field);
            Expression castResult = Expression.Convert(access, typeof(object));
            return Expression.Lambda<Func<object, object?>>(castResult, param).Compile();
        }
        return _ => null;
    }

    private static Action<object, object?> CreateSetter(Type type, string name)
    {
        PropertyInfo? prop = type.GetProperty(name, All);
        if (prop is not null && prop.SetMethod is not null)
        {
            ParameterExpression targetParam = Expression.Parameter(typeof(object), "target");
            ParameterExpression valueParam = Expression.Parameter(typeof(object), "value");
            Expression castTarget = type.IsValueType ? Expression.Unbox(targetParam, type) : Expression.Convert(targetParam, type);
            Expression castValue = Expression.Convert(valueParam, prop.PropertyType);
            Expression call = Expression.Call(castTarget, prop.SetMethod, castValue);
            return Expression.Lambda<Action<object, object?>>(call, targetParam, valueParam).Compile();
        }
        FieldInfo? field = type.GetField(name, All);
        if (field is not null)
        {
            ParameterExpression targetParam = Expression.Parameter(typeof(object), "target");
            ParameterExpression valueParam = Expression.Parameter(typeof(object), "value");
            Expression castTarget = type.IsValueType ? Expression.Unbox(targetParam, type) : Expression.Convert(targetParam, type);
            Expression castValue = Expression.Convert(valueParam, field.FieldType);
            Expression assign = Expression.Assign(Expression.Field(castTarget, field), castValue);
            return Expression.Lambda<Action<object, object?>>(assign, targetParam, valueParam).Compile();
        }
        throw new MissingMemberException(type.FullName, name);
    }

    private static Func<object?> CreateStaticGetter(Type type, string name)
    {
        PropertyInfo? prop = type.GetProperty(name, All);
        if (prop is not null && prop.GetMethod is not null && prop.GetMethod.IsStatic)
        {
            Expression call = Expression.Call(prop.GetMethod);
            Expression castResult = Expression.Convert(call, typeof(object));
            return Expression.Lambda<Func<object?>>(castResult).Compile();
        }
        FieldInfo? field = type.GetField(name, All);
        if (field is not null && field.IsStatic)
        {
            Expression access = Expression.Field(null, field);
            Expression castResult = Expression.Convert(access, typeof(object));
            return Expression.Lambda<Func<object?>>(castResult).Compile();
        }
        return () => null;
    }

    private static Action<object?> CreateStaticSetter(Type type, string name)
    {
        PropertyInfo? prop = type.GetProperty(name, All);
        if (prop is not null && prop.SetMethod is not null && prop.SetMethod.IsStatic)
        {
            ParameterExpression valueParam = Expression.Parameter(typeof(object), "value");
            Expression castValue = Expression.Convert(valueParam, prop.PropertyType);
            Expression call = Expression.Call(prop.SetMethod, castValue);
            return Expression.Lambda<Action<object?>>(call, valueParam).Compile();
        }
        FieldInfo? field = type.GetField(name, All);
        if (field is not null && field.IsStatic)
        {
            ParameterExpression valueParam = Expression.Parameter(typeof(object), "value");
            Expression castValue = Expression.Convert(valueParam, field.FieldType);
            Expression assign = Expression.Assign(Expression.Field(null, field), castValue);
            return Expression.Lambda<Action<object?>>(assign, valueParam).Compile();
        }
        throw new MissingMemberException(type.FullName, name);
    }

    private static MethodInfo FindMethod(Type type, string name, bool isStatic, object?[] args)
    {
        string key = BuildMethodCacheKey(type, name, isStatic, args);
        return MethodCache.GetOrAdd(key, _ =>
        {
            return type.GetMethods(All)
                .Where(method => method.Name == name && method.IsStatic == isStatic && !method.IsGenericMethodDefinition)
                .Where(method => method.GetParameters().Length == args.Length)
                .FirstOrDefault(method => ParametersAccept(method.GetParameters(), args))
                ?? throw new MissingMethodException(type.FullName, name);
        });
    }

    private static string BuildMethodCacheKey(Type type, string name, bool isStatic, object?[] args)
    {
        if (args.Length == 0) return $"{type.FullName}:{name}:{(isStatic ? "S" : "I")}:0";
        return $"{type.FullName}:{name}:{(isStatic ? "S" : "I")}:{args.Length}:{string.Join(';', args.Select(a => a?.GetType().FullName ?? "null"))}";
    }

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
