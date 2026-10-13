// Handle-based NTFS identity for the same-root registry maintenance owner.
// Compiled by the pinned framework csc (C# 5 syntax), never through Add-Type.
// Every fact of one object comes from one handle opened without following a
// reparse point; a content-equal copy has another FileId and is not continuity.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

public sealed class MineruSameRootRead
{
    public byte[] Bytes;
    public string Facts;
}

public static class MineruSameRootIdentity
{
    const uint FILE_READ_ATTRIBUTES = 0x80, READ_CONTROL = 0x20000, GENERIC_READ = 0x80000000;
    const uint SHARE_ALL = 7, OPEN_EXISTING = 3;
    const uint BACKUP_SEMANTICS = 0x02000000, OPEN_REPARSE_POINT = 0x00200000;
    const uint ATTRIBUTE_DIRECTORY = 0x10, ATTRIBUTE_REPARSE_POINT = 0x400;
    const int FileStandardInfo = 1, FileAttributeTagInfo = 9, FileIdInfo = 18;
    const uint OWNER_GROUP_DACL = 1 | 2 | 4;
    const uint VOLUME_NAME_DOS = 0, VOLUME_NAME_GUID = 1;

    [StructLayout(LayoutKind.Sequential)] struct STANDARD {
        public long allocation, endOfFile; public uint links; public byte deletePending, directory;
    }
    [StructLayout(LayoutKind.Sequential)] struct TAG { public uint attributes, reparseTag; }
    // FILE_ID_INFO: 64-bit volume serial and the 128-bit FileId as two little-endian halves.
    [StructLayout(LayoutKind.Sequential)] struct ID { public ulong volume, low, high; }

    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern SafeFileHandle CreateFileW(string path, uint access, uint share, IntPtr security,
        uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool GetFileInformationByHandleEx(SafeFileHandle file, int kind, out STANDARD value, uint size);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool GetFileInformationByHandleEx(SafeFileHandle file, int kind, out TAG value, uint size);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool GetFileInformationByHandleEx(SafeFileHandle file, int kind, out ID value, uint size);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern uint GetFinalPathNameByHandleW(SafeFileHandle file, StringBuilder path, uint capacity, uint flags);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool GetVolumeInformationByHandleW(SafeFileHandle file, StringBuilder volumeName, uint volumeNameSize,
        out uint serial, out uint maximumComponent, out uint flags, StringBuilder fileSystem, uint fileSystemSize);
    [DllImport("advapi32.dll", SetLastError=true)]
    static extern uint GetSecurityInfo(SafeFileHandle handle, int objectType, uint information, out IntPtr owner,
        out IntPtr group, out IntPtr dacl, out IntPtr sacl, out IntPtr descriptor);
    [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool ConvertSecurityDescriptorToStringSecurityDescriptorW(IntPtr descriptor, uint revision,
        uint information, out IntPtr text, out uint length);
    [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr memory);

    static Win32Exception Last(string what, string path) {
        int error = Marshal.GetLastWin32Error();
        return new Win32Exception(error, what + " failed for " + path + " (win32 " + error.ToString(CultureInfo.InvariantCulture) + ")");
    }

    static SafeFileHandle Open(string path, uint access) {
        SafeFileHandle handle = CreateFileW(path, access, SHARE_ALL, IntPtr.Zero, OPEN_EXISTING,
            BACKUP_SEMANTICS | OPEN_REPARSE_POINT, IntPtr.Zero);
        if (handle.IsInvalid) {
            Win32Exception failure = Last("open", path);
            handle.Dispose();
            throw failure;
        }
        return handle;
    }

    static string JsonString(string value) {
        StringBuilder b = new StringBuilder("\"");
        foreach (char ch in value) {
            if (ch == '"' || ch == '\\') b.Append('\\').Append(ch);
            else if (ch < 32 || ch > 126) b.Append("\\u").Append(((int)ch).ToString("x4", CultureInfo.InvariantCulture));
            else b.Append(ch);
        }
        return b.Append('"').ToString();
    }

    static string FinalPath(SafeFileHandle handle, uint flags, string path) {
        StringBuilder text = new StringBuilder(32768);
        uint length = GetFinalPathNameByHandleW(handle, text, (uint)text.Capacity, flags);
        if (length == 0 || length >= text.Capacity) throw Last("final path", path);
        return text.ToString();
    }

    static string Sddl(SafeFileHandle handle, string path) {
        IntPtr owner, group, dacl, sacl, descriptor, text;
        uint length;
        uint error = GetSecurityInfo(handle, 1, OWNER_GROUP_DACL, out owner, out group, out dacl, out sacl, out descriptor);
        if (error != 0) throw new Win32Exception((int)error, "security descriptor read failed for " + path);
        try {
            if (!ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, OWNER_GROUP_DACL, out text, out length))
                throw Last("security descriptor conversion", path);
            try { return Marshal.PtrToStringUni(text); }
            finally { LocalFree(text); }
        } finally { LocalFree(descriptor); }
    }

    // One closed JSON object; the caller compares objects field by field.
    static string Facts(SafeFileHandle handle, string path, bool directory) {
        ID id; TAG tag; STANDARD standard;
        if (!GetFileInformationByHandleEx(handle, FileIdInfo, out id, (uint)Marshal.SizeOf(typeof(ID)))) throw Last("file id", path);
        if (!GetFileInformationByHandleEx(handle, FileAttributeTagInfo, out tag, (uint)Marshal.SizeOf(typeof(TAG)))) throw Last("attributes", path);
        if (!GetFileInformationByHandleEx(handle, FileStandardInfo, out standard, (uint)Marshal.SizeOf(typeof(STANDARD)))) throw Last("standard information", path);
        if ((tag.attributes & ATTRIBUTE_REPARSE_POINT) != 0) throw new IOException("same-root object is a reparse point: " + path);
        bool isDirectory = (tag.attributes & ATTRIBUTE_DIRECTORY) != 0;
        if (isDirectory != directory || (standard.directory != 0) != directory)
            throw new IOException("same-root object kind differs from the expected kind: " + path);
        if (standard.deletePending != 0) throw new IOException("same-root object is pending deletion: " + path);
        StringBuilder volumeName = new StringBuilder(261), fileSystem = new StringBuilder(261);
        uint serial, maximumComponent, flags;
        if (!GetVolumeInformationByHandleW(handle, volumeName, (uint)volumeName.Capacity, out serial, out maximumComponent,
            out flags, fileSystem, (uint)fileSystem.Capacity)) throw Last("volume information", path);
        StringBuilder json = new StringBuilder("{");
        json.Append("\"path\":").Append(JsonString(path));
        json.Append(",\"final_path\":").Append(JsonString(FinalPath(handle, VOLUME_NAME_DOS, path)));
        json.Append(",\"volume_path\":").Append(JsonString(FinalPath(handle, VOLUME_NAME_GUID, path)));
        json.Append(",\"file_system\":").Append(JsonString(fileSystem.ToString()));
        json.Append(",\"volume_serial\":").Append(JsonString(id.volume.ToString("x16", CultureInfo.InvariantCulture)));
        json.Append(",\"file_id\":").Append(JsonString(id.low.ToString("x16", CultureInfo.InvariantCulture)
            + id.high.ToString("x16", CultureInfo.InvariantCulture)));
        json.Append(",\"attributes\":").Append(tag.attributes.ToString(CultureInfo.InvariantCulture));
        json.Append(",\"is_directory\":").Append(isDirectory ? "true" : "false");
        json.Append(",\"link_count\":").Append(standard.links.ToString(CultureInfo.InvariantCulture));
        json.Append(",\"size\":").Append(standard.endOfFile.ToString(CultureInfo.InvariantCulture));
        json.Append(",\"sddl\":").Append(JsonString(Sddl(handle, path)));
        return json.Append('}').ToString();
    }

    public static string Describe(string path, bool directory) {
        using (SafeFileHandle handle = Open(path, FILE_READ_ATTRIBUTES | READ_CONTROL)) {
            return Facts(handle, path, directory);
        }
    }

    // Every ancestor from the volume root down to the parent must be a plain
    // directory: a junction or symlink anywhere would let the path name another tree.
    public static string Ancestors(string path) {
        List<string> chain = new List<string>();
        string current = Path.GetDirectoryName(Path.GetFullPath(path));
        while (current != null) { chain.Add(current); current = Path.GetDirectoryName(current); }
        chain.Reverse();
        StringBuilder json = new StringBuilder("[");
        for (int index = 0; index < chain.Count; index++) {
            using (SafeFileHandle handle = Open(chain[index], FILE_READ_ATTRIBUTES)) {
                ID id; TAG tag;
                if (!GetFileInformationByHandleEx(handle, FileIdInfo, out id, (uint)Marshal.SizeOf(typeof(ID)))) throw Last("file id", chain[index]);
                if (!GetFileInformationByHandleEx(handle, FileAttributeTagInfo, out tag, (uint)Marshal.SizeOf(typeof(TAG)))) throw Last("attributes", chain[index]);
                if ((tag.attributes & ATTRIBUTE_REPARSE_POINT) != 0) throw new IOException("same-root ancestor is a reparse point: " + chain[index]);
                if ((tag.attributes & ATTRIBUTE_DIRECTORY) == 0) throw new IOException("same-root ancestor is not a directory: " + chain[index]);
                if (index > 0) json.Append(',');
                json.Append("{\"path\":").Append(JsonString(chain[index]));
                json.Append(",\"file_id\":").Append(JsonString(id.low.ToString("x16", CultureInfo.InvariantCulture)
                    + id.high.ToString("x16", CultureInfo.InvariantCulture)));
                json.Append(",\"attributes\":").Append(tag.attributes.ToString(CultureInfo.InvariantCulture)).Append('}');
            }
        }
        return json.Append(']').ToString();
    }

    // Bytes and identity of one regular file through the same handle; the facts
    // before and after the read must agree, so a concurrent change is refused.
    public static MineruSameRootRead ReadRegular(string path, int maximumBytes) {
        if (maximumBytes < 1) throw new ArgumentOutOfRangeException("maximumBytes");
        using (SafeFileHandle handle = Open(path, GENERIC_READ | READ_CONTROL | FILE_READ_ATTRIBUTES)) {
            string before = Facts(handle, path, false);
            byte[] bytes;
            string after;
            using (FileStream stream = new FileStream(handle, FileAccess.Read, 4096, false)) {
                long length = stream.Length;
                if (length < 1 || length > maximumBytes) throw new IOException("same-root file is outside its byte bound: " + path);
                bytes = new byte[(int)length];
                int offset = 0;
                while (offset < bytes.Length) {
                    int read = stream.Read(bytes, offset, bytes.Length - offset);
                    if (read <= 0) throw new EndOfStreamException("same-root file ended early: " + path);
                    offset += read;
                }
                if (stream.Read(new byte[1], 0, 1) != 0) throw new IOException("same-root file grew while reading: " + path);
                after = Facts(handle, path, false);
            }
            if (!string.Equals(before, after, StringComparison.Ordinal)) throw new IOException("same-root file changed while reading: " + path);
            MineruSameRootRead result = new MineruSameRootRead();
            result.Bytes = bytes;
            result.Facts = after;
            return result;
        }
    }
}
