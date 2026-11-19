using System;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;

namespace VpnHelper.Interop;

public static class IpHlpApi
{
    private const string DllName = "Iphlpapi.dll";

    [StructLayout(LayoutKind.Explicit)]
    public struct SOCKADDR_INET
    {
        [FieldOffset(0)] public ushort si_family;
        [FieldOffset(4)] public uint Ipv4Address;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct MIB_IPFORWARD_ROW2
    {
        public ulong InterfaceLuid;
        public uint InterfaceIndex;
        public SOCKADDR_INET DestinationPrefixAddress;
        public byte DestinationPrefixLength;
        public SOCKADDR_INET NextHop;
        public uint SitePrefixLength;
        public uint ValidLifetime;
        public uint PreferredLifetime;
        public uint Metric;
        public uint Protocol;
        public byte Loopback;
        public byte AutoconfigureAddress;
        public byte Publish;
        public byte Immortal;
        public uint Age;
        public uint Origin;
    }

    [DllImport(DllName, ExactSpelling = true)]
    public static extern void InitializeIpForwardEntry(out MIB_IPFORWARD_ROW2 row);

    [DllImport(DllName, ExactSpelling = true)]
    public static extern uint CreateIpForwardEntry2(ref MIB_IPFORWARD_ROW2 row);

    [DllImport(DllName, ExactSpelling = true)]
    public static extern uint DeleteIpForwardEntry2(ref MIB_IPFORWARD_ROW2 row);

    [DllImport(DllName, ExactSpelling = true)]
    public static extern uint GetBestRoute2(
        IntPtr interfaceLuid,
        uint interfaceIndex,
        IntPtr sourceAddress,
        ref SOCKADDR_INET destinationAddress,
        uint addressSortOptions,
        out MIB_IPFORWARD_ROW2 bestRoute,
        out SOCKADDR_INET bestSourceAddress
    );

    public static uint IpToUint(string ipAddress)
    {
        var ip = IPAddress.Parse(ipAddress);
        var bytes = ip.GetAddressBytes();
        return BitConverter.ToUInt32(bytes, 0);
    }
}
