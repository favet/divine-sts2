using System;
using System.Reflection;

#nullable disable
Type cr = typeof(MegaCrit.Sts2.Core.Nodes.Rooms.NCombatRoom);
foreach (var p in cr.GetProperties(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance))
{
    if (p.Name.Contains("Combat") || p.Name.Contains("Player") || p.Name.Contains("State") || p.Name.Contains("Creature"))
        Console.WriteLine($"NCombatRoom: {p.PropertyType.Name} {p.Name}");
}
