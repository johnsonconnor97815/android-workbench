import java.lang.reflect.*;
import java.nio.file.*;
public class JDProbe {
  public static void main(String[] args) throws Exception {
    Path directory = Paths.get(args[0]);
    Class<?> loader = Class.forName("org.jd.core.v1.api.loader.Loader");
    Class<?> printer = Class.forName("org.jd.core.v1.api.printer.Printer");
    Object input = Proxy.newProxyInstance(loader.getClassLoader(), new Class<?>[]{loader}, (proxy, method, values) -> {
      Path path = directory.resolve(values[0] + ".class");
      if (method.getName().equals("canLoad")) return Files.isRegularFile(path);
      if (method.getName().equals("load")) return Files.readAllBytes(path);
      throw new UnsupportedOperationException(method.getName());
    });
    StringBuilder text = new StringBuilder();
    Object output = Proxy.newProxyInstance(printer.getClassLoader(), new Class<?>[]{printer}, (proxy, method, values) -> {
      if (values != null && method.getName().startsWith("print"))
        for (Object value : values) if (value instanceof String) text.append(value).append(' ');
      return null;
    });
    Class<?> type = Class.forName("org.jd.core.v1.ClassFileToJavaSourceDecompiler");
    Object decompiler = type.getConstructor().newInstance();
    type.getMethod("decompile", loader, printer, String.class).invoke(decompiler, input, output, "Probe");
    if (!text.toString().contains("STATIC_EXTRA_FIXED_MARKER")) throw new AssertionError("marker lost");
    System.out.println(text);
  }
}
